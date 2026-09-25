"""Утилиты для Chrome / undetected_chromedriver.

Проблема: каждый вызов uc.Chrome() по умолчанию удаляет и заново скачивает
chromedriver с Google Storage. При медленной/зависшей сети это выглядит как
«зависание» на строке launching Chrome до таймаута бота.

Решение: один раз подготовить пропатченный драйвер и переиспользовать его,
плюс глобальный lock, чтобы два воркера не патчили один файл одновременно.
"""

import os
import re
import socket
import subprocess
import sys
import threading
import logging
import time

logger = logging.getLogger(__name__)

_driver_lock = threading.Lock()
_patched_driver_path = None  # type: str | None
_cached_chrome_major = None  # type: int | None


def _from_windows_registry():
    """Версия Chrome из реестра Windows."""
    logger.info("Checking Chrome version from registry...")
    try:
        import winreg
    except ImportError:
        logger.warning("winreg not available")
        return None

    reg_paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Wow6432Node\Google\Chrome\BLBeacon"),
    ]
    for hive, path in reg_paths:
        try:
            key = winreg.OpenKey(hive, path)
            version, _ = winreg.QueryValueEx(key, "version")
            winreg.CloseKey(key)
            if version:
                logger.info(f"Found Chrome version from registry: {version}")
                return version
        except OSError:
            continue
    logger.info("Chrome version not found in registry")
    return None


def _from_executable():
    """Версия Chrome из исполняемого файла (Win/Linux/Mac)."""
    logger.info("Checking Chrome version from executable...")
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            logger.info(f"Found Chrome executable at: {path}")
            try:
                if path.lower().endswith(".exe"):
                    logger.info(f"Getting version via PowerShell for {path}")
                    out = subprocess.run(
                        ["powershell", "-command",
                         f"(Get-Item '{path}').VersionInfo.ProductVersion"],
                        capture_output=True, text=True, timeout=10
                    ).stdout.strip()
                else:
                    logger.info(f"Getting version via --version for {path}")
                    out = subprocess.run(
                        [path, "--version"],
                        capture_output=True, text=True, timeout=10
                    ).stdout.strip()
                match = re.search(r"(\d+)\.\d+\.\d+", out)
                if match:
                    logger.info(f"Found Chrome version from executable: {match.group(0)}")
                    return match.group(0)
            except (subprocess.SubprocessError, OSError) as e:
                logger.warning(f"Failed to get version from {path}: {e}")
                continue
    logger.info("Chrome version not found from executable")
    return None


def get_chrome_major_version():
    """Возвращает мажорную версию установленного Chrome (int) или None."""
    global _cached_chrome_major
    if _cached_chrome_major is not None:
        return _cached_chrome_major

    logger.info("Getting Chrome major version...")
    version = _from_windows_registry() or _from_executable()
    if version:
        try:
            major = int(version.split(".")[0])
            logger.info(f"Chrome major version: {major}")
            _cached_chrome_major = major
            return major
        except (ValueError, IndexError):
            logger.warning(f"Failed to parse version: {version}")
            return None
    logger.info("Could not determine Chrome version")
    return None


def _default_uc_driver_path():
    """Путь к бинарнику, который использует undetected_chromedriver на этой ОС."""
    if os.name == "nt":
        base = os.path.expanduser(r"~\appdata\roaming\undetected_chromedriver")
        return os.path.abspath(os.path.join(base, "undetected_chromedriver.exe"))
    if sys.platform.startswith("darwin"):
        base = os.path.expanduser("~/Library/Application Support/undetected_chromedriver")
        return os.path.abspath(os.path.join(base, "undetected_chromedriver"))
    base = os.path.expanduser("~/.local/share/undetected_chromedriver")
    return os.path.abspath(os.path.join(base, "undetected_chromedriver"))


def _is_patched(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as fh:
            return b"undetected chromedriver" in fh.read()
    except OSError:
        return False


def _driver_major_version(path: str):
    """Мажорная версия chromedriver.exe или None."""
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
        match = re.search(r"ChromeDriver\s+(\d+)", out)
        if match:
            return int(match.group(1))
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning(f"Could not read chromedriver version: {e}")
    return None


def ensure_patched_driver(version_main=None, download_timeout=45):
    """
    Гарантирует наличие пропатченного chromedriver на диске.
    Скачивает только если файла ещё нет, он не пропатчен, или версия не совпадает.
    """
    global _patched_driver_path

    if version_main is None:
        version_main = get_chrome_major_version()

    if _patched_driver_path and _is_patched(_patched_driver_path):
        if version_main is None or _driver_major_version(_patched_driver_path) == version_main:
            return _patched_driver_path

    path = _default_uc_driver_path()
    if _is_patched(path):
        drv_ver = _driver_major_version(path)
        if version_main is None or drv_ver == version_main:
            logger.info(f"Reusing patched chromedriver v{drv_ver}: {path}")
            _patched_driver_path = path
            return path
        logger.info(
            f"Chromedriver v{drv_ver} != Chrome v{version_main}, re-downloading..."
        )

    import undetected_chromedriver as uc

    logger.info(
        f"Preparing chromedriver (download+patch if needed), "
        f"Chrome major={version_main}, timeout={download_timeout}s..."
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(download_timeout)
    t0 = time.time()
    try:
        patcher = uc.Patcher(
            executable_path=None,
            force=True,
            version_main=version_main or 0,
        )
        patcher.auto(force=True, version_main=version_main)
        path = patcher.executable_path
    finally:
        socket.setdefaulttimeout(old_timeout)

    if not _is_patched(path):
        raise RuntimeError(f"Failed to patch chromedriver at {path}")

    logger.info(f"Chromedriver ready in {time.time() - t0:.1f}s: {path}")
    _patched_driver_path = path
    return path


def create_uc_driver(options=None, headless=True, version_main=None,
                     proxy_url=None, user_data_dir=None):
    """
    Создаёт uc.Chrome с переиспользованием уже пропатченного драйвера.

    options — готовый uc.ChromeOptions без --headless; headless передаётся
    отдельным флагом, чтобы UC применил свои патчи для headless.

    proxy_url — например socks5://127.0.0.1:1080 или http://127.0.0.1:7890.
    Если None, берётся PROXY_URL из config/.env.

    user_data_dir — постоянный профиль Chrome: куки/fingerprint сохраняются
    на диске между запусками (важно против Incapsula).
    """
    import undetected_chromedriver as uc

    if version_main is None:
        version_main = get_chrome_major_version()

    if proxy_url is None:
        try:
            from config import PROXY_URL
            proxy_url = PROXY_URL
        except Exception:
            proxy_url = ""

    if options is None:
        options = uc.ChromeOptions()

    if proxy_url:
        # Chrome ждёт host:port или scheme://host:port
        options.add_argument(f"--proxy-server={proxy_url}")
        logger.info(f"Using proxy: {proxy_url}")
    else:
        logger.info("No PROXY_URL set — Chrome uses system network (включите VPN если Copart недоступен)")

    with _driver_lock:
        driver_path = ensure_patched_driver(version_main=version_main)
        logger.info(
            f"Launching Chrome (cached driver, headless={headless}, "
            f"version_main={version_main})..."
        )
        t0 = time.time()
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(60)
        try:
            driver = uc.Chrome(
                options=options,
                driver_executable_path=driver_path,
                version_main=version_main,
                headless=headless,
                patcher_force_close=True,
                use_subprocess=True,
                user_data_dir=user_data_dir,
            )
        finally:
            socket.setdefaulttimeout(old_timeout)

        logger.info(f"Chrome launched in {time.time() - t0:.1f}s")
        return driver


def chrome_user_agent(version_main=None):
    """User-Agent, совпадающий с реальной версией Chrome (важно против Incapsula)."""
    if version_main is None:
        version_main = get_chrome_major_version() or 150
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version_main}.0.0.0 Safari/537.36"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Chrome major version:", get_chrome_major_version())
    print("Patched driver:", ensure_patched_driver())
