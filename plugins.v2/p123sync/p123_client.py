"""Thin wrapper around the ``p123client`` library used by p123sync.

The wrapper keeps the plugin independent from p123disk while reusing the same
proven request patterns:

* lazy login (the client is only constructed on first use);
* automatic re-login when 123 reports "tokens number has exceeded the limit";
* paginated ``fs_list`` with the conservative 1s page interval p123disk uses;
* upload with MD5 instant-upload first, then slice upload with retries.

All methods return plain dicts/lists and raise :class:`P123ClientError` on
failures so the sync engine never has to touch raw API responses.
"""

from __future__ import annotations

import time
import re
import threading
from hashlib import md5
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:
    from p123client import P123Client, check_response
except ImportError as exc:  # pragma: no cover - dependency shipped via requirements
    raise ImportError("p123sync 依赖未安装，请重新安装插件以安装 requirements") from exc


PAGE_SIZE = 100
# p123disk uses 1s between fs_list pages; keep the same conservative pacing to
# avoid tripping 123 web-API rate limits during full-tree listings.
PAGE_INTERVAL_SECONDS = 1.0
# Directory trees can contain thousands of small folders, so pacing only
# subsequent pages does not prevent bursts of first-page requests.
LIST_REQUEST_INTERVAL_SECONDS = 0.2
LIST_RATE_LIMIT_RETRIES = 5
# MD5 is computed with 1MB reads instead of p123disk's 4KB reads.
MD5_CHUNK_SIZE = 1024 * 1024
UPLOAD_RETRIES = 6
UPLOAD_RETRY_WAIT_SECONDS = 10
UPLOAD_TIMEOUT_SECONDS = 300


class P123ClientError(Exception):
    """Raised when a 123 API call fails."""

    def __init__(self, message: str, *, service_code=None, operation=None):
        super().__init__(message)
        self.service_code = service_code
        self.operation = operation


class RemoteItem(dict):
    """One remote file/dir entry, keys: FileId/FileName/Type/Size/UpdateAt."""

    @property
    def file_id(self) -> int:
        return int(self["FileId"])

    @property
    def name(self) -> str:
        return str(self["FileName"])

    @property
    def is_dir(self) -> bool:
        return int(self.get("Type", 0)) == 1

    @property
    def size(self) -> int:
        return int(self.get("Size") or 0)

    @property
    def md5(self) -> Optional[str]:
        value = str(self.get("Etag") or self.get("etag") or "").strip().lower()
        return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


class P123AutoClient:
    """Lazy proxy over :class:`p123client.P123Client` with token re-login.

    Mirrors p123disk's ``tool.P123AutoClient``: every attribute access goes
    through ``__getattr__``, creating the real client on first use and
    rebuilding it once when 123 rejects the token because too many logins
    exist for the account.
    """

    def __init__(self, passport: str, password: str):
        self._passport = passport
        self._password = password
        self._client: Optional[P123Client] = None

    def _build(self) -> P123Client:
        return P123Client(self._passport, self._password)

    def close(self):
        client = self._client
        self._client = None
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def call(self, name: str, *args, **kwargs) -> Any:
        """Invoke a P123Client method with one token-limit re-login retry."""
        try:
            if self._client is None:
                self._client = self._build()
            result = getattr(self._client, name)(*args, **kwargs)
        except Exception as err:
            raise P123ClientError(f"{name} 调用失败（{type(err).__name__}）") from err
        if (
            isinstance(result, dict)
            and result.get("code") == 401
            and result.get("message") == "tokens number has exceeded the limit"
        ):
            self.close()
            try:
                self._client = self._build()
                result = getattr(self._client, name)(*args, **kwargs)
            except Exception as err:
                raise P123ClientError(f"{name} 重连后调用失败（{type(err).__name__}）") from err
        return result


class P123SyncClient:
    """High level operations needed by the sync engine."""

    def __init__(
        self,
        passport: str,
        password: str,
        logger: Optional[Callable[[str], None]] = None,
        page_interval: float = PAGE_INTERVAL_SECONDS,
    ):
        self.client = P123AutoClient(passport, password)
        self._logger = logger or (lambda _msg: None)
        self._page_interval = page_interval
        self._list_lock = threading.Lock()
        self._next_list_at = 0.0

    # ------------------------------------------------------------------ utils

    def _log(self, message: str):
        self._logger(message)

    def _checked(self, name: str, *args, **kwargs) -> Dict:
        try:
            resp = self.client.call(name, *args, **kwargs)
        except P123ClientError:
            raise
        except Exception as err:
            raise P123ClientError(f"{name} 调用失败（{type(err).__name__}）") from err
        try:
            check_response(resp)
        except Exception as err:
            # Response bodies can contain credentials, signed URLs or file
            # details. Preserve only the numeric service code for diagnosis.
            raw_code = resp.get("code") if isinstance(resp, dict) else None
            code = str(raw_code) if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool) else ""
            detail = f"，云盘错误码 {code}" if re.fullmatch(r"-?\d{1,10}", code) else ""
            raise P123ClientError(f"{name} 响应异常（{type(err).__name__}{detail}）",
                                  service_code=int(code) if detail else None, operation=name) from err
        return resp

    def user_info(self) -> Dict[str, Any]:
        resp = self._checked("user_info")
        data = resp.get("data") or {}
        if data.get("SpacePermanent") is None or data.get("SpaceUsed") is None:
            raise P123ClientError("云盘未返回完整的空间用量信息")
        total = int(data["SpacePermanent"]) + int(data.get("SpaceTemp") or 0)
        used = int(data["SpaceUsed"])
        if total < 0 or used < 0:
            raise P123ClientError("云盘返回的空间用量无效")
        return {"total": total, "used": used, "available": total - used}

    def login_ok(self) -> Tuple[bool, str]:
        try:
            info = self.user_info()
        except Exception as err:
            return False, str(err)
        return True, "登录成功"

    # ----------------------------------------------------------------- listing

    def list_dir(self, parent_file_id: int, cancel_check: Optional[Callable[[], bool]] = None) -> List[RemoteItem]:
        """List one directory using the page-based endpoint and its total."""
        return self._list_new(parent_file_id, cancel_check=cancel_check)

    def _list_request(self, payload: Dict, cancel_check: Optional[Callable[[], bool]] = None) -> Dict:
        """Pace list calls across directories and back off on service throttling."""
        for attempt in range(LIST_RATE_LIMIT_RETRIES + 1):
            with self._list_lock:
                self._wait(max(0.0, self._next_list_at - time.monotonic()), cancel_check)
                if cancel_check and cancel_check():
                    raise KeyboardInterrupt
                self._next_list_at = time.monotonic() + LIST_REQUEST_INTERVAL_SECONDS
                try:
                    return self._checked("request", "file/list/new", params=payload)
                except P123ClientError as err:
                    if err.service_code not in (429, 100011) or attempt == LIST_RATE_LIMIT_RETRIES:
                        raise
            delay = min(2 ** (attempt + 1), 20)
            self._log(f"云盘目录请求受限，{delay} 秒后重试")
            self._wait(delay, cancel_check)
        raise AssertionError("unreachable")

    def _list_new(self, parent_file_id: int, search_name: Optional[str] = None,
                  cancel_check: Optional[Callable[[], bool]] = None) -> List[RemoteItem]:
        """Reject incomplete, repeated or changing pages instead of guessing."""
        items: List[RemoteItem] = []
        seen_ids = set()
        expected_total = None
        page = 1
        while True:
            if cancel_check and cancel_check():
                raise KeyboardInterrupt
            payload = {
                "driveId": 0,
                "limit": PAGE_SIZE,
                "Page": page,
                "parentFileId": int(parent_file_id),
                "inDirectSpace": False,
                "orderBy": "file_id",
                "orderDirection": "asc",
                "trashed": False,
                "fileCategory": 0,
                "event": "homeListFile",
                "OnlyLookAbnormalFile": 0,
            }
            if search_name is not None:
                payload["SearchData"] = search_name
            # The p123client fs_list_new wrapper lowercases query keys, while
            # this endpoint requires camelCase (for example driveId). Send the
            # documented keys directly through its authenticated request.
            resp = self._list_request(payload, cancel_check)
            data = resp.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("InfoList"), list) or "Total" not in data:
                raise P123ClientError("云盘目录响应缺少列表或总数，无法确认列举完整")
            try:
                total = int(data["Total"])
                if total < 0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise P123ClientError("云盘目录总数无效，无法确认列举完整") from None
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise P123ClientError("云盘目录在列举期间发生变化，请重试")
            info_list = data["InfoList"]
            for raw in info_list:
                if not isinstance(raw, dict) or raw.get("FileId") is None:
                    raise P123ClientError("云盘目录项目缺少标识，无法确认列举完整")
                file_id = str(raw["FileId"])
                if file_id in seen_ids:
                    raise P123ClientError("云盘目录分页重复，无法确认列举完整")
                seen_ids.add(file_id)
                items.append(RemoteItem(raw))
            if len(items) > total:
                raise P123ClientError("云盘目录数量异常，无法确认列举完整")
            if len(items) == total:
                return items
            if not info_list or str(data.get("Next")) == "-1":
                raise P123ClientError("云盘目录分页中断，无法确认列举完整")
            page += 1
            self._wait(self._page_interval, cancel_check)

    def walk(self, root_file_id: int, prefix: str = "",
             cancel_check: Optional[Callable[[], bool]] = None) -> Iterator[Tuple[str, RemoteItem]]:
        """Yield (relative_path, item) for every file under root, depth-first."""
        stack: List[Tuple[str, int]] = [(prefix, int(root_file_id))]
        while stack:
            if cancel_check and cancel_check():
                raise KeyboardInterrupt
            rel_dir, dir_id = stack.pop()
            for item in self.list_dir(dir_id, cancel_check=cancel_check):
                rel_path = f"{rel_dir}{item.name}"
                if item.is_dir:
                    stack.append((f"{rel_path}/", item.file_id))
                else:
                    yield rel_path, item

    def find_files_by_name(self, parent_file_id: int, name: str,
                           cancel_check: Optional[Callable[[], bool]] = None) -> List[RemoteItem]:
        """Search, then retain exact names in the requested parent only."""
        matches: List[RemoteItem] = []
        for item in self._list_new(parent_file_id, search_name=name, cancel_check=cancel_check):
            try:
                same_parent = int(item.get("ParentFileId")) == int(parent_file_id)
            except (TypeError, ValueError):
                same_parent = False
            if same_parent and not item.is_dir and item.name == name:
                matches.append(item)
        return matches

    # -------------------------------------------------------------- operations

    def mkdir(self, parent_file_id: int, name: str) -> int:
        resp = self._checked("fs_mkdir", name, parent_id=int(parent_file_id))
        data = (resp.get("data") or {}).get("Info") or {}
        file_id = data.get("FileId")
        if file_id is None:
            raise P123ClientError(f"创建目录 {name} 未返回 FileId")
        return int(file_id)

    def trash(self, file_id: int) -> bool:
        self._checked("fs_trash", int(file_id), event="intoRecycle")
        return True

    def rename(self, file_id: int, name: str) -> None:
        # Never ask the remote service to replace a same-name file implicitly.
        self._checked("fs_rename", {"FileId": int(file_id), "fileName": name, "duplicate": 0})

    def file_exists(self, file_id: int) -> bool:
        try:
            self._checked("fs_info", int(file_id))
            return True
        except P123ClientError:
            return False

    def _file_md5(self, path: Path, cancel_check: Optional[Callable[[], bool]] = None,
                  on_progress: Optional[Callable[[int], None]] = None) -> str:
        hasher = md5()
        processed = 0
        last_report = 0.0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(MD5_CHUNK_SIZE), b""):
                if cancel_check and cancel_check():
                    raise KeyboardInterrupt
                hasher.update(chunk)
                processed += len(chunk)
                now = time.monotonic()
                if on_progress and now - last_report >= 0.5:
                    on_progress(processed)
                    last_report = now
        if on_progress:
            on_progress(processed)
        return hasher.hexdigest()

    def _put(self, url: str, data: bytes) -> None:
        response = self.client.call(
            "request",
            url,
            method="PUT",
            headers={"authorization": ""},
            parse=...,
            timeout=UPLOAD_TIMEOUT_SECONDS,
            data=data,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()

    def upload(
        self,
        local_path: Path,
        parent_file_id: int,
        name: str,
        on_progress: Optional[Callable[[float], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        file_md5: Optional[str] = None,
        on_stage: Optional[Callable[[str], None]] = None,
    ) -> Tuple[int, bool]:
        """Upload one file. Returns (remote_file_id, instant_upload)."""
        file_size = local_path.stat().st_size
        file_md5 = file_md5 or self._file_md5(local_path, cancel_check)
        if cancel_check and cancel_check():
            raise KeyboardInterrupt

        # 1) instant upload attempt
        if on_stage:
            on_stage("checking_instant")
        instant_started = time.monotonic()
        resp = self._checked(
            "upload_request",
            {
                "etag": file_md5,
                "fileName": name,
                "size": file_size,
                "parentFileId": int(parent_file_id),
                "type": 0,
                "duplicate": 0,
            },
        )
        data = resp.get("data") or {}
        self._log(f"秒传请求完成: {name}，耗时 {time.monotonic() - instant_started:.1f} 秒，"
                  f"{'命中' if data.get('Reuse') else '未命中'}")
        if data.get("Reuse"):
            info = data.get("Info") or {}
            return int(info["FileId"]), True

        # 2) slice upload
        slice_size = int(data.get("SliceSize") or 0)
        if slice_size <= 0:
            raise P123ClientError("upload_request 未返回有效 SliceSize")
        upload_data: Dict[str, Any] = dict(data)
        instant = False
        upload_started = time.monotonic()

        if file_size > slice_size:
            self._log(f"分片上传: {name}，分片大小 {slice_size / 1024 ** 2:.1f} MB")
            offset = 0
            slice_no = 1
            with open(local_path, "rb") as f:
                def chunks():
                    while True:
                        if cancel_check and cancel_check():
                            raise KeyboardInterrupt
                        if on_stage:
                            on_stage("reading_upload")
                        chunk = f.read(slice_size)
                        if not chunk:
                            return
                        yield chunk

                for chunk in chunks():
                    if cancel_check and cancel_check():
                        raise KeyboardInterrupt
                    num_to_upload = min(slice_size, file_size - offset)
                    upload_data["partNumberStart"] = slice_no
                    upload_data["partNumberEnd"] = slice_no + 1
                    if on_stage:
                        on_stage("preparing_upload")
                    url_resp = self._checked("upload_prepare", upload_data)
                    self._put_with_retry(url_resp, upload_data, str(slice_no), chunk, cancel_check, on_stage)
                    slice_no += 1
                    offset += num_to_upload
                    if on_progress:
                        on_progress(min(offset * 100.0 / file_size, 100.0))
        else:
            if cancel_check and cancel_check():
                raise KeyboardInterrupt
            self._log(f"直传: {name}")
            if on_stage:
                on_stage("preparing_upload")
            auth_resp = self._checked("upload_auth", upload_data)
            if on_stage:
                on_stage("reading_upload")
            self._put_with_retry(auth_resp, upload_data, "1", local_path.read_bytes(), cancel_check, on_stage)
            if on_progress:
                on_progress(100.0)

        upload_data["isMultipart"] = file_size > slice_size
        if cancel_check and cancel_check():
            raise KeyboardInterrupt
        if on_stage:
            on_stage("completing_upload")
        complete = self._checked("upload_complete", upload_data)
        info = ((complete.get("data") or {}).get("file_info")) or {}
        if not info.get("FileId"):
            raise P123ClientError("upload_complete 未返回 FileId")
        self._log(f"云盘上传完成确认: {name}，耗时 {time.monotonic() - upload_started:.1f} 秒")
        return int(info["FileId"]), instant

    def _put_with_retry(
        self,
        url_resp: Dict[str, Any],
        upload_data: Dict[str, Any],
        part_key: str,
        chunk: bytes,
        cancel_check: Optional[Callable[[], bool]] = None,
        on_stage: Optional[Callable[[str], None]] = None,
    ) -> None:
        """PUT one slice, retrying with a fresh presigned URL on failures."""
        current = url_resp
        last_error: Optional[Exception] = None
        for attempt in range(1, UPLOAD_RETRIES + 1):
            if cancel_check and cancel_check():
                raise KeyboardInterrupt
            part_url = (current.get("data") or {}).get("presignedUrls", {}).get(part_key)
            if part_url:
                try:
                    if on_stage:
                        on_stage("uploading")
                    self._put(part_url, chunk)
                    return
                except Exception as err:  # noqa: PERF203 - retry loop
                    last_error = err
                    self._log(f"分片 {part_key} 上传失败 ({attempt}/{UPLOAD_RETRIES}): {type(err).__name__}")
            if attempt < UPLOAD_RETRIES:
                if on_stage:
                    on_stage("upload_retry")
                self._wait(UPLOAD_RETRY_WAIT_SECONDS, cancel_check)
                try:
                    current = self._checked("upload_prepare", upload_data)
                except P123ClientError as err:
                    last_error = err
                    self._log(f"重新获取上传 URL 失败: {type(err).__name__}")
        raise P123ClientError(f"分片 {part_key} 上传失败（已重试 {UPLOAD_RETRIES} 次，{type(last_error).__name__ if last_error else '无可用地址'}）")

    @staticmethod
    def _wait(seconds: float, cancel_check: Optional[Callable[[], bool]] = None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cancel_check and cancel_check():
                raise KeyboardInterrupt
            time.sleep(min(0.2, max(deadline - time.monotonic(), 0)))
