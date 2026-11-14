"""Pinned llama.cpp/GGUF artifacts and resumable, checksum-verified downloads.

No upstream manifests are trusted at runtime. Updating a pin requires reviewing
its release digest (GitHub) or LFS SHA-256 (Hugging Face) in this source file.
"""
from __future__ import annotations

import asyncio
import hashlib
import platform
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from kova.local_models import LocalModelError

RELEASE = 'b12387'
RELEASE_URL = f'https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/'


@dataclass(frozen=True)
class Artifact:
    url: str
    name: str
    size: int
    sha256: str


# GitHub release asset digests, not hashes downloaded beside the executable.
_BUILDS = {
    'macos-arm64': (16_923_428, 'a654e6020f85d71274f1cf29460caec6537db31decc95a1a9e2f91ce05e648d7'),
    'macos-x64': (12_669_627, '624b91b4af91212631a0a2518ba8405b9cd9f14cad50498d27c3d421453d1b3c'),
    'ubuntu-arm64': (18_974_458, 'cb6acd002e11ebdad9eca716c72a7a5275afbb0ae9be36d25a9199bf306a67bf'),
    'ubuntu-x64': (23_337_268, '9c58ebcd5a704e879cfadf2bbcb1fb1ec8ad4a5bec761fc58b3515864dd5e37a'),
    'ubuntu-vulkan-arm64': (29_472_885, 'a30d146ce6178481f66d40f2e9f87cf3ef47d254fcbd5e4d9f330f2f8ae40711'),
    'ubuntu-vulkan-x64': (36_191_448, 'e75a1996f918e94515c6e2f156693c310dd7b0666f635252e8a32d8bb8b9a382'),
    'win-cpu-arm64': (13_466_926, 'd77e3fae0f188e2232b481eda455fca3b43f869ef3a6589e3499964f8df62358'),
    'win-cpu-x64': (25_124_944, 'ced8482efac76c5f24398b97a6fe1662109038ec3b880686fd7c7c8f22bc5d99'),
    'win-vulkan-arm64': (26_052_045, 'd82aab38a46ebaeb174e85487febbc7774da57538adb94c82d57861b674145d1'),
    'win-vulkan-x64': (38_119_681, '68462280e0801af67a5a02617cf155a0fa8018fd5f805b2285157b771c10da12'),
}


def engine_artifact(*, cpu: bool = False) -> Artifact:
    """Choose a CPU, Vulkan or Metal build, never guess an unsupported ABI."""
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    system = platform.system()
    prefix = {'Linux': 'ubuntu' + ('' if cpu else '-vulkan'),
              'Windows': 'win-' + ('cpu' if cpu else 'vulkan'), 'Darwin': 'macos'}.get(system)
    key = f'{prefix}-{arch}'
    if key not in _BUILDS or (system == 'Darwin' and arch == 'x64' and not cpu):
        raise LocalModelError('No pinned managed engine supports this OS/GPU combination. Use CPU mode or install Ollama from https://ollama.com/download and run ollama serve, then retry.')
    size, digest = _BUILDS[key]
    name = f'llama-{RELEASE}-bin-{key}' + ('.zip' if system == 'Windows' else '.tar.gz')
    return Artifact(RELEASE_URL + name, name, size, digest)


def _gguf(repo, revision, filename, size, sha256):
    return Artifact(f'https://huggingface.co/{repo}/resolve/{revision}/{filename}', filename, size, sha256)


MODELS = {
    'qwen3:4b': _gguf('Qwen/Qwen3-4B-GGUF', 'bc640142c66e1fdd12af0bd68f40445458f3869b',
                     'Qwen3-4B-Q4_K_M.gguf', 2_497_280_256,
                     '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'),
    'qwen3:8b': _gguf('Qwen/Qwen3-8B-GGUF', '7c41481f57cb95916b40956ab2f0b139b296d974',
                     'Qwen3-8B-Q4_K_M.gguf', 5_027_783_488,
                     'd98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'),
    'llama3.2:3b': _gguf('bartowski/Llama-3.2-3B-Instruct-GGUF', '5ab33fa94d1d04e903623ae72c95d1696f09f9e8',
                       'Llama-3.2-3B-Instruct-Q4_K_M.gguf', 2_019_377_696,
                       '6c1a2b41161032677be168d354123594c0e6e67d2b9227c84f296ad037c728ff'),
}


def verified(path: Path, artifact: Artifact) -> bool:
    """Check bytes as well as length; a same-sized partial is not a model."""
    if not path.is_file() or path.stat().st_size != artifact.size:
        return False
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest() == artifact.sha256


async def download(client: httpx.AsyncClient, artifact: Artifact, target: Path):
    """Keep .part on interruption; promote only exact pinned length and SHA-256."""
    part = target.with_suffix(target.suffix + '.part')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if await asyncio.to_thread(verified, target, artifact):
            yield {'type': 'progress', 'message': 'Verified cached download.', 'total': artifact.size, 'completed': artifact.size}
            return
        target.unlink(missing_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        if offset >= artifact.size:
            if await asyncio.to_thread(verified, part, artifact):
                part.replace(target)
                yield {'type': 'progress', 'message': 'Verified completed partial download.', 'total': artifact.size, 'completed': artifact.size}
                return
            part.unlink()
            offset = 0
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        async with client.stream('GET', artifact.url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            if response.headers.get('content-encoding', 'identity') != 'identity':
                raise ValueError('encoded download')
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                if not match or tuple(map(int, match.groups())) != (offset, artifact.size - 1, artifact.size):
                    raise ValueError('invalid range')
            elif response.status_code == 200:
                offset = 0  # a host ignoring Range must replace, never append
            else:
                raise ValueError('unexpected download status')
            with part.open('ab' if offset else 'wb') as handle:
                part.chmod(0o600)
                async for chunk in response.aiter_bytes():
                    if offset + len(chunk) > artifact.size:
                        raise ValueError('oversized download')
                    handle.write(chunk)
                    handle.flush()
                    offset += len(chunk)
                    yield {'type': 'progress', 'stage': 'download', 'message': 'Downloading / verifying pinned artifact…',
                           'total': artifact.size, 'completed': offset}
        if not await asyncio.to_thread(verified, part, artifact):
            if part.stat().st_size >= artifact.size:
                part.unlink()  # a bad complete hash cannot be resumed
            raise ValueError('incomplete or corrupt download')
        part.replace(target)
    except (httpx.HTTPError, OSError, ValueError):
        raise LocalModelError('Download could not be verified or the host is unreachable. Check internet access and free disk space, then retry to resume the .part file. '
                              'Alternatively install Ollama from https://ollama.com/download, run ollama serve and retry setup. No ready model was added.') from None


def engine_binary(home: Path, artifact: Artifact) -> Path:
    """Find the server alongside its packaged shared libraries."""
    root = home / 'engine' / (artifact.name + '.d')
    candidates = [p for p in root.rglob('llama-server*') if p.name in ('llama-server', 'llama-server.exe') and p.is_file()]
    if len(candidates) != 1:
        raise LocalModelError('The pinned engine archive has no unique llama-server. Remove the engine cache and retry, or use Ollama.')
    return candidates[0]


def _extract(archive: Path, destination: Path) -> None:
    # Bound expansion and forbid escaping links, special files and paths before
    # extracting anything. Shared-library symlinks inside the archive are valid.
    limit = 2 * 1024 ** 3
    if archive.name.endswith('.zip'):
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if sum(m.file_size for m in members) > limit:
                raise ValueError('archive too large')
            for member in members:
                path = Path(member.filename.replace('\\', '/'))
                if path.is_absolute() or '..' in path.parts or ':' in member.filename or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('unsafe archive member')
            source.extractall(destination)
    else:
        with tarfile.open(archive, 'r:gz') as source:
            members = source.getmembers()
            if sum(m.size for m in members) > limit:
                raise ValueError('archive too large')
            for member in members:
                tarfile.data_filter(member, str(destination))
            source.extractall(destination, members=members, filter='data')


async def install_engine(client: httpx.AsyncClient, artifact: Artifact, home: Path):
    """Verify the archive, then atomically publish the extracted engine bundle."""
    root = home / 'engine'
    archive = root / artifact.name
    destination = root / (artifact.name + '.d')
    staging = root / (artifact.name + '.unpack')
    try:
        async for event in download(client, artifact, archive):
            yield event
        if not (destination / '.complete').is_file():
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            _extract(archive, staging)
            (staging / '.complete').write_text(artifact.sha256)
            shutil.rmtree(destination, ignore_errors=True)
            staging.replace(destination)
        binary = engine_binary(home, artifact)
        binary.chmod(0o700)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
        raise LocalModelError('The engine archive could not be safely installed. Check disk space, remove the engine cache and retry, or use Ollama.') from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
