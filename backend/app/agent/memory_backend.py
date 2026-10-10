"""把当前用户的记忆快照提供给 Deep Agents；不是磁盘，也不重新查询 MySQL。"""

from dataclasses import dataclass, field

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

PROFILE_MEMORY_PATH = "/memories/profile.md"
READ_ONLY_ERROR = "Profile memory is read-only."


@dataclass(frozen=True)
class ProfileMemoryBackend(BackendProtocol):
    """每次创建独立实例，仅持有后端已验证归属的一份文本。没有宿主机访问能力。"""

    profile_text: str = field(repr=False)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        # 只匹配唯一虚拟路径；不解析 ../、不拼磁盘路径、不接受用户指定命名空间。
        return [
            FileDownloadResponse(path=path, content=self.profile_text.encode("utf-8"))
            if path == PROFILE_MEMORY_PATH else FileDownloadResponse(path=path, error="file_not_found")
            for path in paths
        ]

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if file_path != PROFILE_MEMORY_PATH:
            return ReadResult(error="File not found.")
        if limit <= 0:
            return ReadResult(file_data={"content": "", "encoding": "utf-8"})
        lines = self.profile_text.splitlines()
        start = max(offset, 0)
        selected = lines[start:start + limit]
        return ReadResult(
            file_data={"content": "\n".join(selected), "encoding": "utf-8"}, total_lines=len(lines) if selected else None,
            start_line=start + 1 if selected else None, end_line=start + len(selected) if selected else None,
        )

    def ls(self, path: str) -> LsResult:
        if path not in ("/", "/memories", "/memories/"):
            return LsResult(error="Directory not found.")
        return LsResult(entries=[{"path": PROFILE_MEMORY_PATH, "is_dir": False,
                                  "size": len(self.profile_text.encode("utf-8"))}])

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, **kwargs) -> GrepResult:
        return GrepResult(error="Search is not available.")

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(error="Search is not available.")

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=READ_ONLY_ERROR)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=READ_ONLY_ERROR)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=READ_ONLY_ERROR)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    # BackendProtocol 的异步方法在线程中调用以上同步实现；同样没有磁盘/数据库写入。
    # 不继承 SandboxBackendProtocol，也不实现 execute，因此没有命令执行后端。
