"""Local process lock shared by manual and unattended Windows launches."""
from contextlib import contextmanager
from pathlib import Path
import os


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(path):
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a+b')
    locked=False
    try:
        stream.seek(0,os.SEEK_END)
        if stream.tell()==0:
            stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            locked=True
        except OSError as exc:
            raise AlreadyRunning('Бот уже працює з цієї папки. Другу копію не запущено.') from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        stream.close()
