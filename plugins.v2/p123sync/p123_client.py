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
# MD5 is computed with 1MB reads instead of p123disk's 4KB reads.
MD5_CHUNK_SIZE = 1024 * 1024
UPLOAD_RETRIES = 6
UPLOAD_RETRY_WAIT_SECONDS = 10
UPLOAD_TIMEOUT_SECONDS = 300


class P123ClientError(Exception):
    """Raised when a 123 API call fails."""


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
        if self._client is None:
            self._client = self._build()
        try:
            result = getattr(self._client, name)(*args, **kwargs)
        except Exception as err:
            raise P123ClientError(f"{name} 调用失败: {err}") from err
        if (
            isinstance(result, dict)
            and result.get("code") == 401
            and result.get("message") == "tokens number has exceeded the limit"
        ):
            self.close()
            self._client = self._build()
            try:
                result = getattr(self._client, name)(*args, **kwargs)
            except Exception as err:
                raise P123ClientError(f"{name} 重连后调用失败: {err}") from err
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

    # ------------------------------------------------------------------ utils

    def _log(self, message: str):
        self._logger(message)

    def _checked(self, name: str, *args, **kwargs) -> Dict:
        resp = self.client.call(name, *args, **kwargs)
        try:
            check_response(resp)
        except Exception as err:
            raise P123ClientError(f"{name} 响应异常: {err}") from err
        return resp

    def user_info(self) -> Dict[str, Any]:
        resp = self._checked("user_info")
        data = resp.get("data") or {}
        total = int(data.get("SpacePermanent") or 0)
        used = int(data.get("SpaceUsed") or 0)
        return {"total": total, "used": used, "available": total - used}

    def login_ok(self) -> Tuple[bool, str]:
        try:
            info = self.user_info()
        except Exception as err:
            return False, str(err)
        return True, f"剩余空间 {max(info['available'], 0) / 1024 ** 3:.1f} GB"

    # ----------------------------------------------------------------- listing

    def list_dir(self, parent_file_id: int) -> List[RemoteItem]:
        """List one directory (files + subdirs), following Next pagination."""
        items: List[RemoteItem] = []
        page = 1
        next_flag = 0
        while True:
            payload = {
                "limit": PAGE_SIZE,
                "next": next_flag,
                "Page": page,
                "parentFileId": int(parent_file_id),
                "inDirectSpace": "false",
            }
            resp = self._checked("fs_list", payload)
            data = resp.get("data") or {}
            info_list = data.get("InfoList") or []
            if not info_list:
                break
            items.extend(RemoteItem(item) for item in info_list if item)
            if str(data.get("Next")) == "-1":
                break
            page += 1
            next_flag = data.get("Next")
            time.sleep(self._page_interval)
        return items

    def walk(self, root_file_id: int, prefix: str = "") -> Iterator[Tuple[str, RemoteItem]]:
        """Yield (relative_path, item) for every file under root, depth-first."""
        stack: List[Tuple[str, int]] = [(prefix, int(root_file_id))]
        while stack:
            rel_dir, dir_id = stack.pop()
            for item in self.list_dir(dir_id):
                rel_path = f"{rel_dir}{item.name}"
                if item.is_dir:
                    stack.append((f"{rel_path}/", item.file_id))
                else:
                    yield rel_path, item

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

    def file_exists(self, file_id: int) -> bool:
        try:
            self._checked("fs_info", int(file_id))
            return True
        except P123ClientError:
            return False

    def _file_md5(self, path: Path) -> str:
        hasher = md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(MD5_CHUNK_SIZE), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _put(self, url: str, data: bytes) -> None:
        self.client.call(
            "request",
            url,
            method="PUT",
            headers={"authorization": ""},
            parse=...,
            timeout=UPLOAD_TIMEOUT_SECONDS,
            data=data,
        )

    def upload(
        self,
        local_path: Path,
        parent_file_id: int,
        name: str,
        on_progress: Optional[Callable[[float], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[int, bool]:
        """Upload one file. Returns (remote_file_id, instant_upload)."""
        file_size = local_path.stat().st_size
        self._log(f"计算 MD5: {name} ({file_size / 1024 ** 2:.1f} MB)")
        file_md5 = self._file_md5(local_path)

        # 1) instant upload attempt
        resp = self._checked(
            "upload_request",
            {
                "etag": file_md5,
                "fileName": name,
                "size": file_size,
                "parentFileId": int(parent_file_id),
                "type": 0,
                "duplicate": 2,
            },
        )
        data = resp.get("data") or {}
        if data.get("Reuse"):
            info = data.get("Info") or {}
            if on_progress:
                on_progress(100.0)
            return int(info["FileId"]), True

        # 2) slice upload
        slice_size = int(data.get("SliceSize") or 0)
        if slice_size <= 0:
            raise P123ClientError("upload_request 未返回有效 SliceSize")
        upload_data: Dict[str, Any] = dict(data)
        instant = False

        if file_size > slice_size:
            self._log(f"分片上传: {name}，分片大小 {slice_size / 1024 ** 2:.1f} MB")
            offset = 0
            slice_no = 1
            with open(local_path, "rb") as f:
                for chunk in iter(lambda: f.read(slice_size), b""):
                    if cancel_check and cancel_check():
                        raise KeyboardInterrupt
                    num_to_upload = min(slice_size, file_size - offset)
                    upload_data["partNumberStart"] = slice_no
                    upload_data["partNumberEnd"] = slice_no + 1
                    url_resp = self._checked("upload_prepare", upload_data)
                    self._put_with_retry(url_resp, upload_data, str(slice_no), chunk)
                    slice_no += 1
                    offset += num_to_upload
                    if on_progress:
                        on_progress(min(offset * 100.0 / file_size, 100.0))
        else:
            self._log(f"直传: {name}")
            auth_resp = self._checked("upload_auth", upload_data)
            self._put_with_retry(auth_resp, upload_data, "1", local_path.read_bytes())
            if on_progress:
                on_progress(100.0)

        upload_data["isMultipart"] = file_size > slice_size
        complete = self._checked("upload_complete", upload_data)
        info = ((complete.get("data") or {}).get("file_info")) or {}
        if not info.get("FileId"):
            raise P123ClientError("upload_complete 未返回 FileId")
        return int(info["FileId"]), instant

    def _put_with_retry(
        self,
        url_resp: Dict[str, Any],
        upload_data: Dict[str, Any],
        part_key: str,
        chunk: bytes,
    ) -> None:
        """PUT one slice, retrying with a fresh presigned URL on failures."""
        current = url_resp
        last_error: Optional[Exception] = None
        for attempt in range(1, UPLOAD_RETRIES + 1):
            part_url = (current.get("data") or {}).get("presignedUrls", {}).get(part_key)
            if part_url:
                try:
                    self._put(part_url, chunk)
                    return
                except Exception as err:  # noqa: PERF203 - retry loop
                    last_error = err
                    self._log(f"分片 {part_key} 上传失败 ({attempt}/{UPLOAD_RETRIES}): {err}")
            if attempt < UPLOAD_RETRIES:
                time.sleep(UPLOAD_RETRY_WAIT_SECONDS)
                try:
                    current = self._checked("upload_prepare", upload_data)
                except P123ClientError as err:
                    last_error = err
                    self._log(f"重新获取上传 URL 失败: {err}")
        raise P123ClientError(f"分片 {part_key} 上传失败（已重试 {UPLOAD_RETRIES} 次）: {last_error}")
