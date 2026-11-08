import os, sys, json, zipfile, subprocess, shutil, tempfile, re, threading, shlex, struct, uuid
import urllib.request, urllib.error, urllib.parse, hashlib, time, datetime
import http.server, webbrowser, secrets, base64, hmac, math

# ---------- PyInstaller-совместимость ----------
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(sys.argv[0]))

MC_DIR = os.path.join(BASE, "minecraft")
VERSIONS_DIR = os.path.join(MC_DIR, "versions")
LAUNCHER_PROFILES_FILE = os.path.join(MC_DIR, "launcher_profiles.json")
JAVA_DIR = os.path.join(MC_DIR, "java")
LIBS_DIR = os.path.join(MC_DIR, "libraries")
ASSETS_DIR = os.path.join(MC_DIR, "assets")
CONFIG = os.path.join(BASE, "config.json")
THEME_IMG = os.path.join(BASE, "theme.png")


def ensure_launcher_profiles():
    """Создаёт совместимый launcher_profiles.json для внешних установщиков.

    Fabric/Forge/NeoForge ищут этот файл в корневой папке игры. Существующие
    профили и прочие настройки сохраняются; токены авторизации сюда не пишутся.
    """
    os.makedirs(MC_DIR, exist_ok=True)
    data = {"profiles": {}, "settings": {}, "version": 3}
    if os.path.isfile(LAUNCHER_PROFILES_FILE):
        try:
            with open(LAUNCHER_PROFILES_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                data.update(existing)
            else:
                raise ValueError("launcher_profiles.json должен содержать JSON-объект")
        except Exception:
            # Не удаляем потенциально полезный пользовательский файл.
            backup = LAUNCHER_PROFILES_FILE + ".corrupt"
            if os.path.exists(backup):
                backup += "." + str(int(time.time()))
            try:
                shutil.copy2(LAUNCHER_PROFILES_FILE, backup)
            except OSError:
                pass
            data = {"profiles": {}, "settings": {}, "version": 3}

    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {}
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    data.setdefault("version", 3)

    # Atomic write prevents partially-written JSON if the launcher closes mid-save.
    tmp_path = LAUNCHER_PROFILES_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LAUNCHER_PROFILES_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    return LAUNCHER_PROFILES_FILE


def register_launcher_profile(version_id, display_name=None):
    """Добавляет установленную версию в стандартный список профилей лаунчера."""
    if not version_id:
        return
    ensure_launcher_profiles()
    try:
        with open(LAUNCHER_PROFILES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"profiles": {}, "settings": {}, "version": 3}
    if not isinstance(data, dict):
        data = {"profiles": {}, "settings": {}, "version": 3}
    profiles = data.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        data["profiles"] = profiles

    profile_key = "hjiky-" + re.sub(r"[^A-Za-z0-9_.-]", "_", str(version_id))
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    profile = profiles.get(profile_key)
    if not isinstance(profile, dict):
        profile = {"created": now, "type": "custom"}
    version_game_dir = os.path.join(VERSIONS_DIR, str(version_id))
    os.makedirs(version_game_dir, exist_ok=True)
    profile.update({
        "name": display_name or f"Hjiky — {version_id}",
        "type": "custom",
        "lastUsed": now,
        "lastVersionId": str(version_id),
        "gameDir": version_game_dir,
    })
    profiles[profile_key] = profile

    tmp_path = LAUNCHER_PROFILES_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LAUNCHER_PROFILES_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

# Microsoft OAuth public application Client ID (default for new installations).
DEFAULT_MS_CLIENT_ID = ""

DEFAULT_CFG = {"nickname": "Player", "gdrive_link": "", "memory_mb": 2048,
               "java": "Авто", "dark_theme": False, "errors_in_window": False,
               "text_color": "", "bg_color": "", "accent_color": "",
               "ignore_missing_libs": False,
               "debug_console": True, "show_vanilla": True,
               "show_fabric": True, "show_forge": True,
               "auto_java": True, "ms_client_id": DEFAULT_MS_CLIENT_ID, "require_license": False, "show_neoforge": True,
               "graphics_backend": "opengl",
               "ms_accounts": [], "selected_ms_account": ""}

def load_cfg():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            if "memory_gb" in cfg and "memory_mb" not in cfg:
                cfg["memory_mb"] = int(cfg.pop("memory_gb") * 1024)
            for k, v in DEFAULT_CFG.items():
                cfg.setdefault(k, v)
            # Fill an empty value in older config files as well as missing keys.
            if not str(cfg.get("ms_client_id") or "").strip():
                cfg["ms_client_id"] = DEFAULT_MS_CLIENT_ID
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CFG)

def save_cfg(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------- Google Drive ----------
def extract_folder_id(url):
    m = re.search(r"/folders/([\w-]{20,})", url)
    if m:
        return m.group(1)
    raise ValueError("Нужна ссылка на ПАПКУ Google Drive вида https://drive.google.com/drive/folders/ID")

def list_folder_zips(folder_url):
    import gdown
    fid = extract_folder_id(folder_url)
    files = gdown.download_folder(id=fid, skip_download=True, quiet=True)
    result = []
    for f in files or []:
        name = getattr(f, "name", "") or getattr(f, "path", "")
        if str(name).lower().endswith(".zip") and getattr(f, "id", None):
            result.append((str(name), f.id))
    if not result:
        raise RuntimeError("В папке не найдено ZIP-архивов")
    return result

def download_file(file_id, dest):
    import gdown
    gdown.download(id=file_id, output=dest, quiet=False)

# ---------- распаковка версий ----------
def _merge_move(src, dst):
    for item in os.listdir(src):
        s, d = os.path.join(src, item), os.path.join(dst, item)
        if os.path.isdir(s):
            if os.path.isdir(d):
                _merge_move(s, d)
            else:
                shutil.move(s, d)
        else:
            if os.path.exists(d):
                try:
                    os.remove(d)
                except Exception:
                    pass
            shutil.move(s, d)

def flatten_version_dir(target):
    for _ in range(8):
        if not os.path.isdir(target):
            break
        entries = os.listdir(target)
        dirs = [e for e in entries if os.path.isdir(os.path.join(target, e))]
        files = [e for e in entries if os.path.isfile(os.path.join(target, e))]
        if any(f.endswith((".jar", ".json")) for f in files):
            break
        if not dirs:
            break
        base_name = os.path.basename(target)
        if len(dirs) == 1 and dirs[0].lower() in (
                "versions", ".minecraft", "minecraft", base_name, base_name.lower()):
            sub = os.path.join(target, dirs[0])
            _merge_move(sub, target)
            try:
                if not os.listdir(sub):
                    os.rmdir(sub)
            except Exception:
                pass
            continue
        break

def install_version(zip_path, on_progress=None):
    name = os.path.splitext(os.path.basename(zip_path))[0]
    target = os.path.join(VERSIONS_DIR, name)
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        root_real = os.path.realpath(target)
        for member in z.infolist():
            destination = os.path.realpath(os.path.join(target, member.filename))
            try:
                safe = os.path.commonpath((root_real, destination)) == root_real
            except ValueError:
                safe = False
            if not safe:
                raise RuntimeError(f"Небезопасный путь в ZIP-архиве: {member.filename}")
        members = [m for m in z.infolist() if not m.is_dir()]
        total_bytes = sum(max(0, m.file_size) for m in members)
        done_bytes = 0
        infos = z.infolist()
        for index, member in enumerate(infos, 1):
            out_path = os.path.realpath(os.path.join(target, member.filename))
            if member.is_dir():
                os.makedirs(out_path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with z.open(member, "r") as source, open(out_path, "wb") as dest_file:
                shutil.copyfileobj(source, dest_file, length=128 * 1024)
            done_bytes += max(0, member.file_size)
            if on_progress:
                pct = (done_bytes * 100.0 / total_bytes) if total_bytes else (index * 100.0 / max(1, len(infos)))
                on_progress({"type": "extract", "name": os.path.basename(member.filename),
                             "downloaded": done_bytes, "total": total_bytes, "percent": pct,
                             "index": index, "count": len(infos)})
    inner = os.path.join(target, "versions")
    if os.path.isdir(inner):
        for sub in os.listdir(inner):
            src, dst = os.path.join(inner, sub), os.path.join(target, sub)
            if os.path.exists(dst):
                if os.path.isdir(dst):
                    _merge_move(src, dst)
                else:
                    os.remove(dst)
                    shutil.move(src, dst)
            else:
                shutil.move(src, dst)
        try:
            if not os.listdir(inner):
                os.rmdir(inner)
        except Exception:
            pass
    flatten_version_dir(target)
    return name

def installed_versions():
    if not os.path.isdir(VERSIONS_DIR):
        return []
    out = []
    for d in sorted(os.listdir(VERSIONS_DIR)):
        p = os.path.join(VERSIONS_DIR, d)
        if os.path.isdir(p):
            files_found = [f.lower() for _, _, fs in os.walk(p) for f in fs]
            # Fabric profile is JSON + inheritsFrom; its actual client JAR belongs to vanilla.
            if any(f.endswith((".jar", ".json")) for f in files_found):
                out.append(d)
    return out

# ---------- сетевое скачивание с несколькими источниками ----------
MOJANG_META_MIRRORS = [
    "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
]
RESOURCE_BASES = [
    "https://resources.download.minecraft.net/",
    "https://bmclapi2.bangbang93.com/assets/",
]
LIBRARY_BASES = [
    "https://libraries.minecraft.net/",
    "https://repo1.maven.org/maven2/",
    "https://bmclapi2.bangbang93.com/maven/",
    "https://maven.minecraftforge.net/",
    "https://maven.fabricmc.net/",
]

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _download_to(url, dest, expected_sha1=None, timeout=120, on_progress=None):
    """Потоковая загрузка с реальными байтами и процентом."""
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    tmp = dest + ".part"
    digest = hashlib.sha1()
    downloaded = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as f:
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    digest.update(chunk)
                    if on_progress:
                        on_progress({"type": "download", "url": url,
                                     "downloaded": downloaded, "total": total,
                                     "percent": (downloaded * 100.0 / total) if total else None})
        if expected_sha1:
            actual = digest.hexdigest()
            if actual.lower() != expected_sha1.lower():
                raise RuntimeError(f"Контрольная сумма не совпала для {url}: ожидался SHA1 {expected_sha1}, получен {actual}")
        os.replace(tmp, dest)
        if on_progress:
            on_progress({"type": "download_done", "url": url, "downloaded": downloaded, "total": total or downloaded, "percent": 100})
        return dest
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

def download_multi(urls, dest, expected_sha1=None, on_status=None, on_progress=None):
    last_err = None
    for url in urls:
        try:
            if on_status:
                on_status(url)
            _download_to(url, dest, expected_sha1, on_progress=on_progress)
            return dest
        except Exception as ex:
            last_err = ex
    raise RuntimeError(f"Не удалось скачать ни с одного источника:\n" +
                       "\n".join(f"  - {u}" for u in urls) +
                       f"\nПоследняя ошибка: {last_err}")

def _mirror_list(url):
    mirrors = [url]
    for orig, mirror in (
        ("https://piston-meta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://launchermeta.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://piston-data.mojang.com/", "https://bmclapi2.bangbang93.com/"),
        ("https://resources.download.minecraft.net/", "https://bmclapi2.bangbang93.com/assets/"),
        ("https://libraries.minecraft.net/", "https://bmclapi2.bangbang93.com/maven/"),
    ):
        if url.startswith(orig):
            m = mirror + url[len(orig):]
            if m not in mirrors:
                mirrors.append(m)
    return mirrors

# ---------- манифест ванильных версий Mojang ----------
_manifest_cache = None

def fetch_version_manifest(on_status=None):
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    last_err = None
    for url in MOJANG_META_MIRRORS:
        try:
            if on_status:
                on_status(f"Загружаю каталог версий: {url}")
            data = _http_get(url, timeout=20)
            manifest = json.loads(data)
            versions = manifest.get("versions", [])
            _manifest_cache = versions
            return versions
        except Exception as ex:
            last_err = ex
    raise RuntimeError(f"Не удалось загрузить каталог версий Mojang: {last_err}")

def find_vanilla_version(vid):
    versions = fetch_version_manifest()
    for v in versions:
        if v.get("id") == vid:
            return v
    return None

# ---------- автоустановка ванильной версии ----------
def install_vanilla(vid, on_progress=None):
    def prog(msg):
        if on_progress:
            on_progress(msg)

    meta = find_vanilla_version(vid)
    if not meta:
        raise RuntimeError(f"Версия «{vid}» не найдена в каталоге Mojang")
    vurl = meta.get("url")
    if not vurl:
        raise RuntimeError(f"Для «{vid}» нет ссылки на version.json")

    target = os.path.join(VERSIONS_DIR, vid)
    os.makedirs(target, exist_ok=True)
    vjson_path = os.path.join(target, vid + ".json")

    prog(f"Скачиваю version.json для {vid}...")
    download_multi(_mirror_list(vurl), vjson_path, on_progress=on_progress)
    with open(vjson_path, encoding="utf-8") as f:
        vjson = json.load(f)

    jar_path = os.path.join(target, vid + ".jar")
    if not os.path.exists(jar_path):
        cd = (vjson.get("downloads") or {}).get("client") or {}
        if not cd.get("url"):
            raise RuntimeError(f"Для «{vid}» нет ссылки на клиентский jar")
        size_mb = cd.get("size", 0) // (1024 * 1024)
        prog(f"Скачиваю клиент {vid} (~{size_mb} МБ)...")
        if on_progress:
            on_progress({"type": "file_plan", "count": 1, "stage": "Клиент Minecraft"})
        download_multi(_mirror_list(cd["url"]), jar_path, cd.get("sha1"),
                       on_status=lambda u: prog(f"Источник: {u}"), on_progress=on_progress)
    prog(f"Клиент {vid} скачан, докачиваю библиотеки и ресурсы...")

    ensure_resources(vid, target, on_progress=on_progress)
    register_launcher_profile(vid, f"Minecraft {vid} — Hjiky")
    prog(f"Версия {vid} установлена!")
    return vid

# ---------- Fabric ----------
FABRIC_META = "https://meta.fabricmc.net/v2/versions"

def fetch_fabric_versions(on_status=None):
    """[(mc_version, loader_version), ...] для релизов 1.14+."""
    data = json.loads(_http_get(FABRIC_META + "/loader", timeout=20))
    loader = next((v["version"] for v in data if v.get("stable")), data[0]["version"])
    manifest = fetch_version_manifest()
    out = []
    for v in manifest:
        if v.get("type") != "release":
            continue
        m = re.match(r"1\.(\d+)", v["id"])
        if m and int(m.group(1)) >= 14:
            out.append((v["id"], loader))
    return out

def install_fabric(mc_ver, on_progress=None):
    ensure_launcher_profiles()
    def prog(m):
        if on_progress:
            on_progress(m)
    prog(f"Загружаю данные Fabric для {mc_ver}...")
    loaders = json.loads(_http_get(f"{FABRIC_META}/versions/loader/{mc_ver}", timeout=20))
    if not loaders:
        raise RuntimeError(f"Fabric пока не поддерживает {mc_ver}")
    loader_ver = loaders[0]["loader"]["version"]

    vid = f"fabric-{mc_ver}"
    target = os.path.join(VERSIONS_DIR, vid)
    os.makedirs(target, exist_ok=True)
    jpath = os.path.join(target, vid + ".json")

    # Скачиваем ОФИЦИАЛЬНЫЙ профиль Fabric — он содержит все библиотеки
    # (sponge-mixin, asm, fabric-loader, intermediary) и inheritsFrom.
    # Самодельный json без mixin вызывал NoClassDefFoundError: MixinBootstrap.
    prog(f"Скачиваю официальный профиль Fabric {loader_ver}...")
    profile_url = f"{FABRIC_META}/versions/loader/{mc_ver}/{loader_ver}/profile/json"
    vjson = json.loads(_http_get(profile_url, timeout=30))
    # принудительно ставим наш id (meta отдаёт id "fabric-loader-...")
    vjson["id"] = vid
    if "inheritsFrom" not in vjson:
        vjson["inheritsFrom"] = mc_ver
    # прописываем требуемую Java для этой версии MC
    minor = int(re.match(r"1\.(\d+)", mc_ver).group(1))
    java_need = 17 if minor >= 18 else 8
    vjson["javaVersion"] = {"component": "java-runtime-gamma",
                            "majorVersion": java_need}
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(vjson, f, indent=2)

    # Fabric не имеет своего jar — jar возьмётся от ванилы
    if not os.path.exists(os.path.join(VERSIONS_DIR, mc_ver, mc_ver + ".jar")):
        prog(f"Fabric требует ванилу {mc_ver} — устанавливаю её...")
        install_vanilla(mc_ver, on_progress=on_progress)

    # докачиваем библиотеки Fabric (mixin, asm и т.д.)
    ensure_resources(vid, target, on_progress=on_progress)
    register_launcher_profile(vid, f"Fabric {loader_ver} — Minecraft {mc_ver}")
    prog(f"Fabric {loader_ver} для {mc_ver} установлена!")
    return vid

# ---------- Forge ----------
def fetch_forge_versions(mc_ver, on_status=None):
    try:
        xml = _http_get(
            "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml",
            timeout=20).decode("utf-8", "replace")
    except Exception as ex:
        raise RuntimeError(f"Не удалось загрузить список Forge: {ex}")
    vers = re.findall(r"<version>([\w.\-]+)</version>", xml)
    out = [v for v in vers if v.startswith(mc_ver + "-")]
    if not out:
        raise RuntimeError(f"Forge не поддерживает {mc_ver}")
    return sorted(out, key=lambda v: [int(x) if x.isdigit() else x
                                      for x in re.split(r"[.\-]", v)])

def install_forge(mc_ver, forge_full=None, on_progress=None):
    ensure_launcher_profiles()
    def prog(m):
        if on_progress:
            on_progress(m)
    if not forge_full:
        forge_full = fetch_forge_versions(mc_ver)[-1]
    vjson_v = find_vanilla_version(mc_ver)
    if not vjson_v:
        raise RuntimeError(f"Forge {forge_full}: ванила {mc_ver} не найдена в каталоге Mojang")
    if not os.path.exists(os.path.join(VERSIONS_DIR, mc_ver, mc_ver + ".jar")):
        prog(f"Forge требует ванилу {mc_ver} — устанавливаю её...")
        install_vanilla(mc_ver, on_progress=on_progress)
    url = (f"https://maven.minecraftforge.net/net/minecraftforge/forge/"
           f"{forge_full}/forge-{forge_full}-installer.jar")
    tmp = os.path.join(tempfile.gettempdir(), f"forge-{forge_full}-installer.jar")
    prog(f"Скачиваю Forge installer {forge_full}...")
    download_multi(_mirror_list(url), tmp, on_progress=on_progress)
    # Forge 1.17+ требует Java 17+, 1.16 и старше — Java 8
    try:
        installed_meta = read_version_json(os.path.join(VERSIONS_DIR, mc_ver), mc_ver)
    except Exception:
        installed_meta = None
    need = required_java_major(installed_meta, mc_ver)
    jexe = None
    for _, p in list_javas():
        if java_major_of(p) == need:
            jexe = p
            break
    if not jexe and cfg.get("auto_java", True):
        jexe = download_java(need, on_progress=on_progress)
    jexe = jexe or "java"
    prog("Запускаю Forge installer (--installClient)...")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run([jexe, "-jar", tmp, "--installClient", MC_DIR],
                       capture_output=True, text=True, errors="replace",
                       timeout=600, creationflags=flags)
    debug_log(f"forge installer exit={r.returncode}\n"
              f"{(r.stdout or '')[-3000:]}\n{(r.stderr or '')[-3000:]}")
    vid = f"{mc_ver}-forge-{forge_full.split('-')[-1]}"
    vdir = os.path.join(VERSIONS_DIR, vid)
    if not os.path.isdir(vdir):
        cands = [d for d in os.listdir(VERSIONS_DIR) if d.startswith(mc_ver + "-forge")]
        if not cands:
            raise RuntimeError(f"Forge installer не создал версию. Код: {r.returncode}. "
                               f"Подробности в консоли отладки.")
        vid = cands[0]
        vdir = os.path.join(VERSIONS_DIR, vid)
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass
    ensure_resources(vid, vdir, on_progress=on_progress)
    register_launcher_profile(vid, f"Forge {forge_full}")
    prog(f"Forge {forge_full} установлена!")
    return vid

# ---------- NeoForge ----------
NEOFORGE_VERSIONS_CACHE = None

def fetch_neoforge_versions(mc_ver):
    """Return installer versions matching Minecraft major/minor (e.g. 1.21.1 -> 21.1)."""
    global NEOFORGE_VERSIONS_CACHE
    if NEOFORGE_VERSIONS_CACHE is None:
        try:
            xml = _http_get("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
                            timeout=25).decode("utf-8", "replace")
            NEOFORGE_VERSIONS_CACHE = re.findall(r"<version>([0-9.]+)</version>", xml)
        except Exception as ex:
            raise RuntimeError(f"Не удалось получить каталог NeoForge: {ex}")
    versions = NEOFORGE_VERSIONS_CACHE
    parts = mc_ver.split(".")
    if len(parts) < 2:
        return []
    prefix = f"{int(parts[1])}.{int(parts[2]) if len(parts)>2 else 0}."
    return sorted([v for v in versions if v.startswith(prefix)],
                  key=lambda v: tuple(int(x) for x in v.split(".")))


def install_neoforge(mc_ver, neo_ver=None, on_progress=None):
    ensure_launcher_profiles()
    if mc_ver.startswith("neoforge-"):
        mc_ver = mc_ver[len("neoforge-"):]
    prog = on_progress or (lambda _m: None)
    versions = fetch_neoforge_versions(mc_ver)
    if not versions:
        raise RuntimeError(f"NeoForge не найден для Minecraft {mc_ver}")
    neo_ver = neo_ver or versions[-1]
    if not os.path.exists(os.path.join(VERSIONS_DIR, mc_ver, mc_ver + ".jar")):
        prog(f"NeoForge требует Minecraft {mc_ver} — устанавливаю ванилу...")
        install_vanilla(mc_ver, on_progress=on_progress)
    url = (f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{neo_ver}/"
           f"neoforge-{neo_ver}-installer.jar")
    tmp = os.path.join(tempfile.gettempdir(), f"neoforge-{neo_ver}-installer.jar")
    prog(f"Скачиваю NeoForge {neo_ver}...")
    download_multi(_mirror_list(url), tmp, on_progress=on_progress)
    meta = read_version_json(os.path.join(VERSIONS_DIR, mc_ver), mc_ver)
    need = required_java_major(meta, mc_ver)
    jexe = next((p for _, p in list_javas() if java_major_of(p) == need), None)
    if not jexe and cfg.get("auto_java", True):
        jexe = download_java(need, on_progress=on_progress)
    if not jexe:
        jexe = "java"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([jexe, "-jar", tmp, "--installClient", MC_DIR],
                            capture_output=True, text=True, errors="replace", timeout=600,
                            creationflags=flags)
    debug_log(f"NeoForge installer exit={result.returncode}\n{(result.stdout or '')[-2500:]}\n{(result.stderr or '')[-2500:]}")
    candidates = [d for d in os.listdir(VERSIONS_DIR)
                  if d.startswith(mc_ver + "-neoforge")]
    if not candidates:
        raise RuntimeError("NeoForge installer не создал профиль. Проверь журнал отладки.")
    vid = sorted(candidates)[-1]
    ensure_resources(vid, os.path.join(VERSIONS_DIR, vid), on_progress=on_progress)
    register_launcher_profile(vid, f"NeoForge {neo_ver}")
    try:
        os.remove(tmp)
    except OSError:
        pass
    prog(f"NeoForge {neo_ver} установлена!")
    return vid


# ---------- запуск игры: поиск version.json / jar ----------
def read_version_json(vdir, version):
    for cand in (os.path.join(vdir, version + ".json"),
                 os.path.join(VERSIONS_DIR, version + ".json")):
        if os.path.exists(cand):
            with open(cand, encoding="utf-8") as f:
                return json.load(f)
    exact = version + ".json"
    for root_, _, files in os.walk(vdir):
        if exact in files:
            p = os.path.join(root_, exact)
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return None

def find_main_jar(vdir, version):
    cand = os.path.join(vdir, version + ".jar")
    if os.path.exists(cand):
        return cand
    inner = os.path.join(vdir, "versions")
    if os.path.isdir(inner):
        for sub in os.listdir(inner):
            c = os.path.join(inner, sub, sub + ".jar")
            if os.path.exists(c):
                return c
    exact = version + ".jar"
    for root_, _, files in os.walk(vdir):
        if exact in files:
            return os.path.join(root_, exact)
    for root_, _, files in os.walk(vdir):
        for f in files:
            if f.endswith(".jar") and re.search(version.replace(".", r"\."), f):
                return os.path.join(root_, f)
    return None

# ---------- библиотеки / нативы ----------
def lib_artifact_path(lib):
    name = lib.get("name", "")
    m = re.match(r"([\w.-]+):([\w.-]+):([\w.+-]+)(?:-([\w.-]+))?", name)
    if not m:
        return None
    grp, art, ver, cls = m.groups()
    # jinput-platform is a Maven POM (not a regular class JAR). Its usable
    # files are the OS-specific natives classifiers handled below.
    if art == "jinput-platform" and not cls:
        return None
    artifact_path = lib.get("downloads", {}).get("artifact", {}).get("path")
    if artifact_path:
        return os.path.join(LIBS_DIR, *artifact_path.split("/"))
    tail = f"-{cls}" if cls else ""
    return os.path.join(LIBS_DIR, *grp.split("."), art, ver, f"{art}-{ver}{tail}.jar")


def current_native_classifier():
    if sys.platform.startswith("win"):
        return "natives-windows"
    if sys.platform == "darwin":
        return "natives-osx"
    return "natives-linux"


def _native_classifier_from_metadata(lib):
    """Resolve legacy Mojang `natives` map, including ${arch} placeholders."""
    if sys.platform.startswith("win"):
        os_key, default = "windows", "natives-windows"
    elif sys.platform == "darwin":
        os_key, default = "osx", "natives-osx"
    else:
        os_key, default = "linux", "natives-linux"
    natives = lib.get("natives") or {}
    classifier = natives.get(os_key) or default
    arch = "64" if struct.calcsize("P") * 8 >= 64 else "32"
    return str(classifier).replace("${arch}", arch)


def _jinput_platform_native_info(lib):
    """Return fallback Maven coordinate for the JInput platform native JAR."""
    name = lib.get("name", "")
    m = re.match(r"([\w.-]+):jinput-platform:([\w.+-]+)(?:-([\w.-]+))?$", name)
    if not m:
        return None
    group, ver, named_classifier = m.groups()
    classifier = named_classifier or _native_classifier_from_metadata(lib)
    rel = (f"{group.replace('.', '/')}/jinput-platform/{ver}/"
           f"jinput-platform-{ver}-{classifier}.jar")
    local = os.path.join(LIBS_DIR, *rel.split("/"))
    repo = (lib.get("url") or "https://libraries.minecraft.net/").rstrip("/") + "/"
    bases = [repo] + [b for b in LIBRARY_BASES if b != repo]
    return [base + rel for base in bases], local, None


def lib_natives_path(lib, os_name=None):
    dl = lib.get("downloads", {})
    classifier = os_name or current_native_classifier()
    classifiers = dl.get("classifiers") or {}
    candidates = [classifier]
    if classifier == "natives-osx":
        candidates.append("natives-macos")
    for key in candidates:
        item = classifiers.get(key)
        if item and item.get("path"):
            return os.path.join(LIBS_DIR, *item["path"].split("/"))
    # Some legacy version JSONs provide only the `natives` map and omit
    # downloads.classifiers. Build the canonical JInput native artifact path.
    jinput = _jinput_platform_native_info(lib)
    if jinput:
        return jinput[1]
    name = lib.get("name", "")
    if classifier in name or (classifier == "natives-osx" and "natives-macos" in name):
        return lib_artifact_path(lib)
    return None

def extract_natives(vdir, libs):
    natives_dir = os.path.join(vdir, "natives")
    if os.path.isdir(natives_dir):
        shutil.rmtree(natives_dir, ignore_errors=True)
    os.makedirs(natives_dir, exist_ok=True)
    extracted = 0
    for lib in libs:
        nl = str(lib.get("name", "")).lower()
        if not any(k in nl for k in ("lwjgl", "jinput", "natives", "openal", "jna")):
            continue
        candidates = []
        np = lib_natives_path(lib)
        if np:
            candidates.append(np)
        ap = lib_artifact_path(lib)
        if ap and "natives" in os.path.basename(ap).lower():
            candidates.append(ap)
        for p in candidates:
            if p and os.path.exists(p) and zipfile.is_zipfile(p):
                try:
                    with zipfile.ZipFile(p) as z:
                        for n in z.namelist():
                            if n.endswith((".dll", ".so", ".dylib", ".jnilib")):
                                z.extract(n, natives_dir)
                                extracted += 1
                except Exception:
                    pass
    return natives_dir, extracted

def collect_jars(*dirs):
    out = []
    for d in dirs:
        if d and os.path.isdir(d):
            for root_, _, files in os.walk(d):
                for f in sorted(files):
                    if f.endswith(".jar"):
                        p = os.path.join(root_, f)
                        if p not in out:
                            out.append(p)
    return out

# ---------- merge inheritsFrom (совместимость Fabric/Forge) ----------
def merge_version_json(vjson, version, depth=0):
    """Разворачивает inheritsFrom: наследует libs/assets/mainClass от ванилы."""
    parent = vjson.get("inheritsFrom")
    if not parent or depth > 4:
        return vjson
    pvjson = read_version_json(os.path.join(VERSIONS_DIR, parent), parent)
    if not pvjson:
        raise RuntimeError(
            f"Не найдена родительская версия «{parent}». "
            f"Установи ванильную версию {parent} (двойной клик по ней в списке).")
    pvjson = merge_version_json(pvjson, parent, depth + 1)
    merged = dict(pvjson)
    merged.update(vjson)
    merged["libraries"] = pvjson.get("libraries", []) + vjson.get("libraries", [])
    if "arguments" in vjson or "arguments" in pvjson:
        pa, ca = pvjson.get("arguments", {}), vjson.get("arguments", {})
        merged["arguments"] = {
            "game": pa.get("game", []) + ca.get("game", []),
            "jvm": pa.get("jvm", []) + ca.get("jvm", []),
        }
        merged.pop("minecraftArguments", None)
    if "assetIndex" not in merged:
        merged["assetIndex"] = pvjson.get("assetIndex")
    return merged

def _sub_args(args_list, subs):
    out = []
    for a in args_list:
        if isinstance(a, str):
            for k, v in subs.items():
                a = a.replace("${" + k + "}", str(v))
            out.append(a)
        elif isinstance(a, dict) and "value" in a:
            if _rules_allow(a.get("rules", [])):
                v = a["value"]
                values = v if isinstance(v, list) else [v]
                for value in values:
                    if isinstance(value, str):
                        for k, replacement in subs.items():
                            value = value.replace("${" + k + "}", str(replacement))
                    out.append(value)
    return out


_SINGLE_VALUE_GAME_OPTIONS = {
    "--version", "--username", "--uuid", "--gameDir", "--assetsDir",
    "--assetIndex", "--accessToken", "--userType", "--versionType",
    "--clientId", "--xuid", "--userProperties", "--width", "--height",
    "--graphicsBackend",
}

def _dedupe_single_value_options(args_list):
    """Keep the last value for singleton game options in inherited profiles."""
    result = []
    positions = {}
    i = 0
    while i < len(args_list):
        token = args_list[i]
        if token in _SINGLE_VALUE_GAME_OPTIONS and i + 1 < len(args_list):
            if token in positions:
                old = positions[token]
                result[old] = None
                if old + 1 < len(result):
                    result[old + 1] = None
            positions[token] = len(result)
            result.extend((token, args_list[i + 1]))
            i += 2
        else:
            result.append(token)
            i += 1
    return [arg for arg in result if arg is not None]


def _remove_unsupported_quickplay_options(args_list):
    """Remove Quick Play selectors: this launcher has no Quick Play UI.

    Version JSON inheritance can concatenate parent and child game arguments.
    Minecraft accepts only one of the Quick Play selectors, so passing inherited
    selectors together crashes during argument parsing. Remove selectors and
    their values while preserving unrelated arguments.
    """
    selectors = {
        "--quickPlaySingleplayer",
        "--quickPlayMultiplayer",
        "--quickPlayRealms",
    }
    result = []
    skip_next = False
    for token in args_list:
        if skip_next:
            skip_next = False
            continue
        if token in selectors:
            skip_next = True
            continue
        # Quick Play path is meaningful only with a selector; this launcher
        # does not expose Quick Play, so omit it too.
        if token == "--quickPlayPath":
            skip_next = True
            continue
        result.append(token)
    return result


def _safe_player_uuid(account, player_name):
    """Return a valid 32-hex UUID; use deterministic offline UUID if absent."""
    raw = str((account or {}).get("uuid") or "").replace("-", "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return raw.lower()
    if (account or {}).get("access_token"):
        raise RuntimeError(
            "У выбранного Microsoft-аккаунта некорректный UUID профиля. "
            "Выйдите из аккаунта и войдите снова."
        )
    return uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + str(player_name)).hex


def build_launch_cmd(version, nickname, mem_mb, vdir, account=None):
    raw = read_version_json(vdir, version)
    if raw is None:
        raise RuntimeError(f"Не найден version.json для «{version}» в {vdir}")
    vjson = merge_version_json(raw, version)
    main_jar = find_main_jar(vdir, version)
    if not main_jar:
        parent = raw.get("inheritsFrom")
        if parent:
            main_jar = find_main_jar(os.path.join(VERSIONS_DIR, parent), parent)
    cp, libs, ignored = [], [], []
    libs = [l for l in vjson.get("libraries", [])
            if _rules_allow(l.get("rules"))]
    for l in libs:
        p = lib_artifact_path(l)
        if p and os.path.exists(p) and p not in cp:
            cp.append(p)
        elif p and not os.path.exists(p):
            ignored.append(os.path.basename(p))
    for p in collect_jars(os.path.join(vdir, "libraries")):
        if p not in cp:
            cp.append(p)
    main_class = vjson.get("mainClass", "net.minecraft.client.main.Main")
    asset_index = (vjson.get("assetIndex") or {}).get("id", "legacy")
    if not main_jar:
        raise RuntimeError("Не найден .jar версии в " + vdir)
    if main_jar not in cp:
        cp.append(main_jar)
    natives, n_extracted = extract_natives(vdir, libs)
    java_exe = get_java_path(cfg.get("java", "Авто"), version, vdir)
    cp_str = os.pathsep.join(cp)
    account = account or {}
    player_name = account.get("username") or nickname
    access_token = account.get("access_token") or "0"
    # Every installed version gets its own game directory. This isolates mods,
    # saves, resourcepacks, shaderpacks, config, screenshots and other game data.
    version_game_dir = os.path.abspath(vdir)
    os.makedirs(version_game_dir, exist_ok=True)
    subs = {"auth_player_name": player_name, "version_name": version,
            "game_directory": version_game_dir, "assets_root": ASSETS_DIR,
            "assets_index_name": asset_index, "auth_access_token": access_token,
            "auth_session": access_token, "user_type": account.get("user_type", "legacy"),
            "version_type": "Launcher", "auth_uuid": _safe_player_uuid(account, player_name),
            "clientid": account.get("client_id", ""), "auth_xuid": account.get("xuid", ""),
            "launcher_name": "hjiky", "launcher_version": "1.0",
            "user_properties": "{}", "natives_directory": natives,
            "classpath": cp_str, "library_directory": LIBS_DIR,
            "classpath_separator": os.pathsep}
    args = vjson.get("arguments", {})
    if args:
        jvm_args = _sub_args(args.get("jvm", []), subs)
        game_args = _sub_args(args.get("game", []), subs)
    else:
        jvm_args = [f"-Djava.library.path={natives}",
                    f"-Dorg.lwjgl.librarypath={natives}",
                    f"-Dnet.java.games.input.librarypath={natives}"]
        mc_args = vjson.get("minecraftArguments", "")
        if mc_args:
            # Legacy version JSON already contains the complete game arguments.
            # Do not prepend defaults: that duplicates --version/--username etc.
            game_args = _sub_args(shlex.split(mc_args), subs)
        else:
            game_args = ["--username", player_name, "--version", version,
                         "--gameDir", version_game_dir, "--assetsDir", ASSETS_DIR,
                         "--assetIndex", asset_index, "--accessToken", access_token,
                         "--versionType", "Launcher"]
    # Graphics API is a game argument supported starting with Minecraft 26.2.
    # Older versions do not understand --graphicsBackend, so leave their args alone.
    # Put the selected value last so it overrides an inherited/profile value.
    version_match = re.match(r"^(\d+)\.(\d+)", str(version))
    if version_match:
        major, minor = (int(part) for part in version_match.groups())
        if major > 26 or (major == 26 and minor >= 2):
            backend = str(cfg.get("graphics_backend", "opengl")).strip().lower()
            if backend not in {"default", "opengl", "vulkan"}:
                backend = "opengl"
            game_args.extend(["--graphicsBackend", backend])
    game_args = _dedupe_single_value_options(game_args)
    game_args = _remove_unsupported_quickplay_options(game_args)
    if "-cp" not in jvm_args and cp_str not in jvm_args:
        jvm_args += ["-cp", cp_str]
    cmd = [java_exe, f"-Xmx{int(mem_mb)}M"] + jvm_args + [main_class] + game_args
    return cmd, n_extracted, java_exe, ignored

def _rules_allow(rules):
    """Evaluate Mojang's ordered allow/disallow rules for the current OS."""
    import platform
    if not rules:
        return True
    os_name = "windows" if sys.platform.startswith("win") else (
        "osx" if sys.platform == "darwin" else "linux")
    machine = platform.machine().lower()
    arch = "x86" if machine in ("i386", "i686", "x86") else (
        "arm64" if machine in ("arm64", "aarch64") else "x86_64")
    allowed = False
    for rule in rules:
        rule_os = rule.get("os") or {}
        if rule_os.get("name") and rule_os["name"] != os_name:
            continue
        if rule_os.get("arch") and rule_os["arch"] != arch:
            continue
        if rule.get("action") == "allow":
            allowed = True
        elif rule.get("action") == "disallow":
            allowed = False
    return allowed

# ---------- Java: определение версии, подбор, автозагрузка ----------
def java_major_of(javaw_exe):
    root = os.path.dirname(os.path.dirname(javaw_exe))
    rel = os.path.join(root, "release")
    if os.path.exists(rel):
        try:
            with open(rel, encoding="utf-8", errors="replace") as f:
                txt = f.read()
            m = re.search(r'JAVA_VERSION="?1?\.?(\d+)', txt)
            if m:
                v = int(m.group(1))
                return 8 if v == 8 else v
        except Exception:
            pass
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([javaw_exe, "-version"], capture_output=True,
                             text=True, errors="replace", timeout=15,
                             creationflags=flags)
        txt = (out.stdout or "") + (out.stderr or "")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', txt)
        if m:
            if m.group(1) == "1":
                return int(m.group(2))
            return int(m.group(1))
    except Exception:
        pass
    return None

def list_javas():
    found = []
    if os.path.isdir(JAVA_DIR):
        for d in sorted(os.listdir(JAVA_DIR)):
            jdir = os.path.join(JAVA_DIR, d)
            if not os.path.isdir(jdir):
                continue
            candidates = (("bin/javaw.exe", "bin/java.exe", "bin/java")
                          if os.name == "nt" else ("bin/java",))
            for exe in candidates:
                p = os.path.join(jdir, exe)
                if os.path.exists(p):
                    found.append((d, p))
                    break
    return found

def required_java_major(vjson, version):
    """Prefer official javaVersion metadata; otherwise use conservative MC defaults."""
    if vjson:
        major = (vjson.get("javaVersion") or {}).get("majorVersion")
        if major:
            return int(major)
    m = re.search(r"(?:^|fabric-)(1\.(\d+)(?:\.(\d+))?)", version)
    if not m:
        # Для новых форматов версий без javaVersion запрашиваем Java у пользователя.
        return None
    minor, patch = int(m.group(2)), int(m.group(3) or 0)
    if minor <= 16:
        return 8
    if minor == 17:
        return 16
    if minor <= 19:
        return 17
    if minor == 20 and patch < 5:
        return 17
    return 21


def _java_platform():
    import platform
    os_id = "windows" if sys.platform.startswith("win") else ("mac" if sys.platform == "darwin" else "linux")
    machine = (os.environ.get("PROCESSOR_ARCHITECTURE") or platform.machine()).lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x64"
    return os_id, arch


def _adoptium_urls(major):
    os_id, arch = _java_platform()
    return [
        f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{os_id}/{arch}/jre/hotspot/normal/eclipse",
        f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{os_id}/{arch}/jdk/hotspot/normal/eclipse",
    ]

def download_java(major, on_progress=None):
    def prog(m):
        if on_progress:
            on_progress(m)
    jname = f"Java{major}"
    jroot = os.path.join(JAVA_DIR, jname)
    os.makedirs(jroot, exist_ok=True)
    tmp_zip = os.path.join(tempfile.gettempdir(), f"temurin-{major}.zip")
    prog(f"Скачиваю Java {major} (Temurin)...")
    download_multi(_adoptium_urls(major), tmp_zip,
                   on_status=lambda u: prog(f"Источник Java: {u}"), on_progress=on_progress)
    prog(f"Распаковываю Java {major}...")
    with zipfile.ZipFile(tmp_zip) as z:
        root_real = os.path.realpath(jroot)
        for member in z.infolist():
            dest = os.path.realpath(os.path.join(jroot, member.filename))
            try:
                safe = os.path.commonpath((root_real, dest)) == root_real
            except ValueError:
                safe = False
            if not safe:
                raise RuntimeError(f"Небезопасный путь в архиве Java: {member.filename}")
        z.extractall(jroot)
    os.remove(tmp_zip)
    entries = os.listdir(jroot)
    dirs = [e for e in entries if os.path.isdir(os.path.join(jroot, e))]
    if len(dirs) == 1 and not os.path.exists(os.path.join(jroot, "bin")):
        _merge_move(os.path.join(jroot, dirs[0]), jroot)
        try:
            os.rmdir(os.path.join(jroot, dirs[0]))
        except Exception:
            pass
    exe_names = ("javaw.exe", "java.exe") if os.name == "nt" else ("java",)
    java_bin = next((os.path.join(jroot, "bin", n) for n in exe_names
                     if os.path.isfile(os.path.join(jroot, "bin", n))), None)
    if not java_bin:
        raise RuntimeError(f"Java {major} скачана, но исполняемый файл не найден в {jroot}")
    prog(f"Java {major} установлена в {jroot}")
    return java_bin

_java_path_cache = {}

def get_java_path(selected, version, vdir=None):
    """Строгий подбор: только точная требуемая major-версия.
    Старые версии НЕ должны запускаться на новой Java (и наоборот).
    Если точной нет и включён auto_java — скачиваем нужную."""
    vjson = read_version_json(vdir, version) if vdir else None
    if vjson and vjson.get("inheritsFrom"):
        try:
            vjson = merge_version_json(vjson, version)
        except Exception as ex:
            debug_log(f"Не удалось объединить метаданные Java для {version}: {ex}")
    need = required_java_major(vjson, version)

    # Пользовательский путь к Java всегда используем напрямую.
    if selected and selected != "Авто" and os.path.isfile(selected):
        return os.path.abspath(selected)
    if need is None and (not selected or selected == "Авто"):
        raise RuntimeError(
            f"JAVA_MANUAL_REQUIRED: Для версии {version} в JSON не указана javaVersion. "
            "Выбери javaw.exe или java.exe вручную."
        )

    javas = list_javas()
    system_java = shutil.which("javaw" if os.name == "nt" else "java") or shutil.which("java")
    if system_java and not any(p == system_java for _, p in javas):
        javas.append(("System PATH", system_java))
    # Ручной выбор имеет приоритет; неизвестную требуемую версию не блокируем.
    if selected and selected != "Авто":
        for name, p in javas:
            if selected.lower() in name.lower() or name.lower() in selected.lower():
                major = java_major_of(p)
                if need is None or major == need:
                    return p
                debug_log(f"Выбранная Java «{name}» ({major}) несовместима с {version} "
                          f"(нужна Java {need}) — подбираю автоматически")
                break

    def major_of_cached(p):
        if p not in _java_path_cache:
            _java_path_cache[p] = java_major_of(p)
        return _java_path_cache[p]

    # ТОЛЬКО точное совпадение. Более новая Java для старых MC даёт
    # ClassFormatError / NoClassDefFoundError, более старая — UnsupportedClassVersion.
    for name, p in javas:
        if major_of_cached(p) == need:
            return p

    if need is None:
        raise RuntimeError(
            f"JAVA_MANUAL_REQUIRED: Не удалось определить требуемую Java для {version}. "
            "Выбери javaw.exe или java.exe вручную."
        )

    if cfg.get("auto_java", True):
        try:
            return download_java(need, on_progress=lambda m: root.after(
                0, lambda msg=m: debug_log(msg)))
        except Exception as ex:
            debug_log(f"Не удалось автоматически установить Java {need}: {ex}")
            # fallback: более новую Java можно брать только для need >= 17
            # (Java 17+ обратно совместима), для need 8 — только Java 8
            if need >= 17:
                higher = sorted(((major_of_cached(p), n, p) for n, p in javas
                                 if (major_of_cached(p) or 0) >= need), reverse=True)
                if higher:
                    return higher[0][2]
    else:
        debug_log(f"Java {need} не найдена, а автозагрузка отключена. "
                  f"Доступные: " + ", ".join(f"{n}({major_of_cached(p)})"
                                             for n, p in javas))
    raise RuntimeError(
        f"Для Minecraft {version} требуется Java {need}, но совместимая Java не найдена. "
        "Включи автоматическую установку Java или установи её вручную.")

# ---------- проверка и автоустановка ресурсов ----------
def _lib_download_info(lib):
    name = lib.get("name", "")
    coord = re.match(r"([\w.-]+):([\w.-]+):([\w.+-]+)(?:-([\w.-]+))?", name)
    # jinput-platform:2.0.5 is a POM-only Maven module, not a plain JAR.
    # Its platform binaries are downloaded separately as native classifiers.
    if coord and coord.group(2) == "jinput-platform" and not coord.group(4):
        return None
    dl = lib.get("downloads", {})
    art = dl.get("artifact") or {}
    if art.get("url"):
        path = art.get("path") or lib_artifact_path(lib)
        if path:
            return _mirror_list(art["url"]), os.path.join(LIBS_DIR, *path.split("/")), art.get("sha1")
    if not coord:
        return None
    grp, arti, ver, cls = coord.groups()
    tail = f"-{cls}" if cls else ""
    rel = f"{grp.replace('.', '/')}/{arti}/{ver}/{arti}-{ver}{tail}.jar"
    local = os.path.join(LIBS_DIR, *rel.split("/"))
    if os.path.exists(local):
        return None
    # приоритет — репозиторий, указанный в самой библиотеке (Fabric/Forge так делают)
    urls = []
    lib_url = lib.get("url")
    if lib_url:
        urls.append(lib_url.rstrip("/") + "/" + rel)
    urls += [base + rel for base in LIBRARY_BASES]
    return urls, local, None


def _natives_download_info(lib):
    dl = lib.get("downloads", {})
    classifier = current_native_classifier()
    classifiers = dl.get("classifiers") or {}
    item = classifiers.get(classifier)
    if not item and classifier == "natives-osx":
        item = classifiers.get("natives-macos")
    if item and item.get("url") and item.get("path"):
        return (_mirror_list(item["url"]),
                os.path.join(LIBS_DIR, *item["path"].split("/")), item.get("sha1"))

    name = lib.get("name", "")
    # Older Mojang JSON can omit downloads.classifiers but still declare the
    # required native classifier in `natives`; synthesize the JInput native URL.
    jinput_info = _jinput_platform_native_info(lib)
    if jinput_info:
        return None if os.path.exists(jinput_info[1]) else jinput_info
    # Legacy Maven coordinates may encode the native classifier in the artifact name.
    if classifier in name or (classifier == "natives-osx" and "natives-macos" in name):
        art = dl.get("artifact") or {}
        if art.get("url") and art.get("path"):
            return (_mirror_list(art["url"]),
                    os.path.join(LIBS_DIR, *art["path"].split("/")), art.get("sha1"))
        m = re.match(r"([\\w.-]+):([\\w.-]+):([\\w.-]+)-((?:natives-windows|natives-linux|natives-osx|natives-macos)(?:-[\\w.-]+)?)", name)
        if m:
            grp, arti, ver, cls = m.groups()
            rel = f"{grp.replace('.', '/')}/{arti}/{ver}/{arti}-{ver}-{cls}.jar"
            local = os.path.join(LIBS_DIR, *rel.split("/"))
            if os.path.exists(local):
                return None
            bases = [f"https://repo1.maven.org/maven2/", "https://libraries.minecraft.net/",
                     "https://bmclapi2.bangbang93.com/maven/"]
            return ([base + rel for base in bases], local, None)
    return None

def missing_libs(vjson):
    missing = []
    for lib in vjson.get("libraries", []):
        if not _rules_allow(lib.get("rules")):
            continue
        info = _lib_download_info(lib)
        if info and not os.path.exists(info[1]):
            missing.append(info)
        ninfo = _natives_download_info(lib)
        if ninfo and not os.path.exists(ninfo[1]):
            missing.append(ninfo)
    return missing

def missing_assets(vjson, on_progress=None):
    ai = vjson.get("assetIndex") or {}
    url = ai.get("url")
    index_id = ai.get("id", "legacy")
    if not url:
        return 0
    idx_dir = os.path.join(ASSETS_DIR, "indexes")
    idx_path = os.path.join(idx_dir, index_id + ".json")
    os.makedirs(idx_dir, exist_ok=True)
    if not os.path.exists(idx_path):
        download_multi(_mirror_list(url), idx_path, ai.get("sha1"), on_progress=on_progress)
    with open(idx_path, encoding="utf-8") as f:
        index = json.load(f)
    objects = index.get("objects", {})
    obj_dir = os.path.join(ASSETS_DIR, "objects")
    downloaded = 0
    pending = []
    for key, obj in objects.items():
        h = obj.get("hash")
        if not h:
            continue
        dest = os.path.join(obj_dir, h[:2], h)
        if os.path.exists(dest) and os.path.getsize(dest) == obj.get("size", -1):
            continue
        pending.append((h, dest))
    if pending and on_progress:
        on_progress({"type": "file_plan", "count": len(pending), "stage": "Ресурсы Minecraft"})
    for h, dest in pending:
        urls = [base + f"{h[:2]}/{h}" for base in RESOURCE_BASES]
        download_multi(urls, dest, h, on_progress=on_progress)
        downloaded += 1
    return downloaded

def ensure_resources(version, vdir, on_progress=None):
    vjson = read_version_json(vdir, version)
    if vjson is None:
        if cfg.get("ignore_missing_libs"):
            if on_progress:
                on_progress("version.json не найден — запускаю без проверки библиотек (игнор включён)")
            return True
        raise RuntimeError(
            f"Для версии «{version}» не найден version.json в {vdir}.\n"
            "Без него нельзя докачать библиотеки и assets.\n"
            "Как исправить: 1) положи в ZIP версии файл <имя>.json (как у официальных "
            "версий Mojang) и переустанови её; или 2) укажи в ZIP полную сборку "
            "с папкой libraries внутри (например, как в сборках TLauncher); "
            "или 3) включи в настройках «Игнорировать отсутствие библиотек».")
    main_jar = find_main_jar(vdir, version)
    if main_jar is None:
        parent = vjson.get("inheritsFrom")
        if parent:
            main_jar = find_main_jar(os.path.join(VERSIONS_DIR, parent), parent)
    if main_jar is None:
        if cfg.get("ignore_missing_libs"):
            if on_progress:
                on_progress("Файл .jar не найден, но запускаю (игнор включён)")
        else:
            raise RuntimeError(
                f"В папке версии {vdir} не найден .jar файл.\n"
                "Как исправить: переустанови версию из ZIP (внутри должен лежать "
                f"{version}.jar или versions/{version}/{version}.jar).")

    problems = []
    try:
        missing = missing_libs(vjson)
        if missing:
            if on_progress:
                on_progress(f"Докачиваю библиотеки: {len(missing)} шт.")
                on_progress({"type": "file_plan", "count": len(missing), "stage": "Библиотеки"})
            for urls, path, sha1 in missing:
                try:
                    download_multi(urls, path, sha1, on_progress=on_progress)
                except Exception as ex:
                    problems.append(f"  • {os.path.basename(path)}\n"
                                    f"    URL:\n      " + "\n      ".join(urls) +
                                    f"\n    Причина: {ex}")
    except Exception as ex:
        if not cfg.get("ignore_missing_libs"):
            raise RuntimeError(
                f"Не удалось докачать библиотеки для «{version}».\n"
                f"Причина: {ex}\n"
                "Как исправить:\n"
                "  1) Проверь интернет-соединение.\n"
                "  2) Проверь, не блокирует ли антивирус/файрвол доступ к "
                "libraries.minecraft.net (добавь лаунчер в исключения).\n"
                "  3) Попробуй запустить лаунчер от имени администратора — возможно, "
                "нет прав на запись в " + LIBS_DIR + ".")
        else:
            if on_progress:
                on_progress(f"Игнор: ошибка докачки библиотек пропущена ({ex})")

    try:
        n = missing_assets(vjson, on_progress=on_progress)
        if n and on_progress:
            on_progress(f"Докачано файлов ресурсов (assets): {n}")
    except Exception as ex:
        if not cfg.get("ignore_missing_libs"):
            raise RuntimeError(
                f"Не удалось докачать ресурсы (assets) для «{version}».\n"
                f"Причина: {ex}\n"
                "Как исправить:\n"
                "  1) Проверь интернет-соединение.\n"
                "  2) Проверь доступ к resources.download.minecraft.net (антивирус/прокси).\n"
                "  3) Убедись, что есть права на запись в " + ASSETS_DIR + ".")
        else:
            if on_progress:
                on_progress(f"Игнор: ошибка докачки assets пропущена ({ex})")

    if problems:
        if cfg.get("ignore_missing_libs"):
            if on_progress:
                on_progress(f"Игнор: {len(problems)} библиотек не скачалось — запуск продолжится")
            debug_log("Пропущенные библиотеки:\n" + "\n".join(problems))
        else:
            raise RuntimeError(
                f"Часть библиотек для «{version}» не скачалась ({len(problems)} шт.):\n"
                + "\n".join(problems)
                + "\n\nКак исправить:\n"
                  "  1) Проверь интернет и повтори запуск.\n"
                  "  2) Проверь, не блокирует ли антивирус/файрвол libraries.minecraft.net.\n"
                  "  3) Запусти лаунчер от имени администратора.")
    return True


# ---------- Microsoft / Xbox / Minecraft account authentication ----------
MS_DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
ACCOUNT_KEYRING_SERVICE = "hjiky-minecraft-launcher"
active_account = None


def _post_json(url, payload, form=False):
    data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
    headers = {"User-Agent": "hjiky-launcher/1.1",
               "Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode("utf-8", "replace")[:2000]
        # Microsoft device-code flow requires the app registration to allow public clients.
        if ex.code == 400 and ("AADSTS70002" in detail or
                               "client application must be marked as 'mobile'" in detail.lower()):
            raise RuntimeError(
                "Microsoft отклонил вход по коду (AADSTS70002). Это настройка приложения, "
                "а не ошибка аккаунта Minecraft. Открой Microsoft Entra admin center → "
                "App registrations → выбери приложение с этим Client ID → Authentication → "
                "Advanced settings → включи 'Allow public client flows' (Yes) и сохрани. "
                "Затем повтори вход по коду. Если у тебя нет доступа к регистрации этого "
                "приложения, используй вход через браузер или укажи Client ID приложения, "
                "в котором разрешены public client flows.\n\nПодробности: " + detail
            ) from ex
        raise RuntimeError(f"Ошибка сервиса авторизации HTTP {ex.code}: {detail}") from ex


def microsoft_device_login(client_id, on_status=None):
    """Microsoft device-code OAuth followed by Xbox/XSTS and official MC entitlement checks."""
    if not client_id or not re.fullmatch(r"[0-9a-fA-F-]{16,}", client_id.strip()):
        raise RuntimeError("Укажи корректный Microsoft Client ID публичного приложения в настройках.")
    report = on_status or (lambda _message: None)
    device = _post_json(MS_DEVICE_CODE_URL, {
        "client_id": client_id.strip(), "scope": "XboxLive.signin offline_access"
    }, form=True)
    report(f"Открой {device.get('verification_uri', 'https://microsoft.com/devicelogin')} "
           f"и введи код {device['user_code']}")
    interval = max(3, int(device.get("interval", 5)))
    deadline = time.time() + int(device.get("expires_in", 900))
    oauth = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            oauth = _post_json(MS_TOKEN_URL, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id.strip(), "device_code": device["device_code"]
            }, form=True)
            break
        except RuntimeError as ex:
            msg = str(ex).lower()
            if "authorization_pending" in msg:
                continue
            if "slow_down" in msg:
                interval += 5
                continue
            raise
    if not oauth or not oauth.get("access_token"):
        raise RuntimeError("Время ожидания авторизации Microsoft истекло.")
    xbl = _post_json(XBL_AUTH_URL, {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + oauth["access_token"]},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"
    })
    xsts = _post_json(XSTS_AUTH_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"
    })
    xui = ((xsts.get("DisplayClaims") or {}).get("xui") or [{}])[0]
    uhs = xui.get("uhs")
    if not uhs:
        raise RuntimeError("Xbox XSTS не вернул user hash.")
    mc = _post_json(MC_LOGIN_URL, {"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    mc_token = mc.get("access_token")
    if not mc_token:
        raise RuntimeError("Minecraft Services не выдал access token.")
    def get_mc_json(url):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mc_token})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            raise RuntimeError(f"Проверка Minecraft Services не пройдена (HTTP {ex.code}).") from ex
    entitlements = get_mc_json(MC_ENTITLEMENTS_URL)
    if not entitlements.get("items"):
        raise RuntimeError("У аккаунта не обнаружена лицензия Minecraft Java Edition.")
    profile = get_mc_json(MC_PROFILE_URL)
    if not profile.get("id") or not profile.get("name"):
        raise RuntimeError("У аккаунта нет готового профиля Minecraft Java Edition.")
    report("Вход выполнен: " + profile["name"])
    return {"username": profile["name"], "uuid": profile["id"],
            "access_token": mc_token, "user_type": "msa",
            "xuid": str(xui.get("xid", "")), "client_id": client_id.strip(),
            "refresh_token": oauth.get("refresh_token", "")}




def _minecraft_account_from_oauth(client_id, oauth, on_status=None):
    """Exchanges Microsoft OAuth access token for a licensed Minecraft Java session."""
    report = on_status or (lambda _message: None)
    if not oauth.get("access_token"):
        raise RuntimeError("Microsoft не вернул OAuth access token.")
    xbl = _post_json(XBL_AUTH_URL, {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + oauth["access_token"]},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"
    })
    xsts = _post_json(XSTS_AUTH_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"
    })
    xui = ((xsts.get("DisplayClaims") or {}).get("xui") or [{}])[0]
    uhs = xui.get("uhs")
    if not uhs:
        raise RuntimeError("Xbox XSTS не вернул user hash.")
    mc = _post_json(MC_LOGIN_URL, {"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    mc_token = mc.get("access_token")
    if not mc_token:
        raise RuntimeError("Minecraft Services не выдал access token.")

    def get_mc_json(url):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mc_token})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Проверка Minecraft Services не пройдена (HTTP {ex.code}): {detail}") from ex

    entitlements = get_mc_json(MC_ENTITLEMENTS_URL)
    if not entitlements.get("items"):
        raise RuntimeError("У аккаунта не обнаружена лицензия Minecraft Java Edition.")
    profile = get_mc_json(MC_PROFILE_URL)
    if not profile.get("id") or not profile.get("name"):
        raise RuntimeError("У аккаунта нет готового профиля Minecraft Java Edition.")
    report("Вход выполнен: " + profile["name"])
    return {"username": profile["name"], "uuid": profile["id"],
            "access_token": mc_token, "user_type": "msa",
            "xuid": str(xui.get("xid", "")), "client_id": client_id.strip(),
            "refresh_token": oauth.get("refresh_token", "")}


def microsoft_browser_login(client_id, on_status=None, timeout=300):
    """System-browser OAuth Authorization Code + PKCE with local loopback callback."""
    if not client_id or not re.fullmatch(r"[0-9a-fA-F-]{16,}", client_id.strip()):
        raise RuntimeError("Укажи корректный Microsoft Client ID публичного приложения в настройках.")
    report = on_status or (lambda _message: None)

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

    callback = {"result": None, "event": threading.Event()}

    class OAuthHTTPServer(http.server.HTTPServer):
        allow_reuse_address = True

    class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            received_state = (params.get("state") or [""])[0]
            if parsed.path != "/" or not hmac.compare_digest(received_state, state):
                body = ("<html><body><h3>Ответ не принят</h3>"
                        "<p>Вернись к странице входа Microsoft и продолжи авторизацию.</p></body></html>")
                encoded = body.encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                # Ignore unsolicited local requests; keep waiting for the state-bound callback.
                return

            oauth_error = (params.get("error") or [""])[0]
            if oauth_error:
                callback["result"] = {
                    "error": oauth_error,
                    "error_description": (params.get("error_description") or [""])[0]
                }
                heading = "Вход отменён или не выполнен"
                message = "Вернись в лаунчер. При необходимости попробуй ещё раз."
                code = 400
            else:
                code_value = (params.get("code") or [""])[0]
                callback["result"] = {"code": code_value} if code_value else {"error": "missing_code"}
                heading = "Авторизация завершена"
                message = "Можно закрыть эту вкладку и вернуться в лаунчер."
                code = 200 if code_value else 400
            body = ("<!doctype html><html><head><meta charset='utf-8'><title>Hjiky Launcher</title></head>"
                    "<body style='font-family:Segoe UI,Arial,sans-serif;background:#171923;color:#f5f5f5;"
                    "display:flex;justify-content:center;padding-top:12vh'>"
                    f"<main><h2>{heading}</h2><p>{message}</p></main></body></html>")
            encoded = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
            callback["event"].set()

        def log_message(self, _format, *_args):
            return

    try:
        server = OAuthHTTPServer(("localhost", 0), OAuthCallbackHandler)
    except OSError as ex:
        raise RuntimeError(f"Не удалось запустить локальный callback-сервер авторизации: {ex}") from ex

    server.timeout = 1
    redirect_uri = f"http://localhost:{server.server_port}"
    authorize_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
        "client_id": client_id.strip(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": "XboxLive.signin offline_access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })

    try:
        report("Открываю браузер Microsoft — выбери аккаунт и подтверди вход…")
        opened = webbrowser.open(authorize_url, new=1, autoraise=True)
        if not opened:
            report("Не удалось автоматически открыть браузер. Открой ссылку входа вручную.")
            raise RuntimeError("Не удалось открыть системный браузер для входа Microsoft.")
        deadline = time.monotonic() + max(30, int(timeout))
        while not callback["event"].is_set() and time.monotonic() < deadline:
            server.handle_request()
        if not callback["event"].is_set():
            raise RuntimeError("Время ожидания ответа браузера истекло. Попробуй войти ещё раз.")
        result = callback["result"] or {}
        if result.get("error"):
            description = result.get("error_description") or result["error"]
            raise RuntimeError("Microsoft не завершил вход: " + description[:500])

        code_value = result.get("code")
        if not code_value:
            raise RuntimeError("Браузер вернул пустой код авторизации.")
        report("Получен ответ Microsoft. Проверяю Xbox и лицензию Minecraft…")
        oauth = _post_json(MS_TOKEN_URL, {
            "grant_type": "authorization_code",
            "client_id": client_id.strip(),
            "code": code_value,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "scope": "XboxLive.signin offline_access",
        }, form=True)
        return _minecraft_account_from_oauth(client_id, oauth, on_status=report)
    finally:
        try:
            server.server_close()
        except Exception:
            pass

def microsoft_refresh_login(client_id, refresh_token):
    oauth = _post_json(MS_TOKEN_URL, {
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": refresh_token, "scope": "XboxLive.signin offline_access"
    }, form=True)
    xbl = _post_json(XBL_AUTH_URL, {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + oauth["access_token"]},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"
    })
    xsts = _post_json(XSTS_AUTH_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"
    })
    xui = ((xsts.get("DisplayClaims") or {}).get("xui") or [{}])[0]
    uhs = xui.get("uhs")
    if not uhs:
        raise RuntimeError("Xbox XSTS не вернул user hash.")
    mc = _post_json(MC_LOGIN_URL, {"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    mc_token = mc.get("access_token")
    if not mc_token:
        raise RuntimeError("Minecraft Services не выдал access token.")
    def get_json(url):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mc_token})
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    ent = get_json(MC_ENTITLEMENTS_URL)
    if not ent.get("items"):
        raise RuntimeError("У аккаунта не обнаружена лицензия Minecraft Java Edition.")
    profile = get_json(MC_PROFILE_URL)
    if not profile.get("id") or not profile.get("name"):
        raise RuntimeError("Профиль Minecraft Java Edition не найден.")
    return {"username": profile["name"], "uuid": profile["id"],
            "access_token": mc_token, "user_type": "msa",
            "xuid": str(xui.get("xid", "")), "client_id": client_id,
            "refresh_token": oauth.get("refresh_token", refresh_token)}

def get_account_skins(account):
    if not account or not account.get("access_token"):
        raise RuntimeError("Сначала войди в лицензионный Microsoft-аккаунт.")
    req = urllib.request.Request(MC_PROFILE_URL, headers={"Authorization": "Bearer " + account["access_token"]})
    with urllib.request.urlopen(req, timeout=25) as response:
        profile = json.loads(response.read().decode("utf-8"))
    return profile.get("skins", [])


def upload_account_skin(account, image_path, variant="classic"):
    if not account or not account.get("access_token"):
        raise RuntimeError("Для смены скина нужен лицензионный Microsoft-аккаунт.")
    if variant not in ("classic", "slim"):
        raise ValueError("Тип модели должен быть classic или slim")
    ext = os.path.splitext(image_path)[1].lower()
    if ext != ".png":
        raise RuntimeError("Выбери PNG-файл скина (64×64 или 64× skin legacy layout).")
    boundary = "----LauncherSkin" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    with open(image_path, "rb") as f:
        image_data = f.read()
    if len(image_data) > 2 * 1024 * 1024:
        raise RuntimeError("Файл скина слишком большой (максимум 2 МБ).")
    if len(image_data) < 24 or image_data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Файл повреждён или не является PNG.")
    width = int.from_bytes(image_data[16:20], "big")
    height = int.from_bytes(image_data[20:24], "big")
    if (width, height) not in ((64, 64), (64, 32)):
        raise RuntimeError(f"Неверный размер скина: {width}×{height}. Нужен 64×64 или 64×32.")
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="variant"\r\n\r\n{variant}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="skin.png"\r\n'
            f"Content-Type: image/png\r\n\r\n").encode() + image_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request("https://api.minecraftservices.com/minecraft/profile/skins", data=body,
        headers={"Authorization": "Bearer " + account["access_token"],
                 "Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode("utf-8", "replace")[:700]
        raise RuntimeError(f"Minecraft не принял скин (HTTP {ex.code}): {detail}") from ex


def _account_key(uuid, field):
    safe_uuid = re.sub(r"[^A-Za-z0-9_-]", "", str(uuid or ""))
    return f"account:{safe_uuid}:{field}"


def save_account_refresh_token(account):
    """Save each account's refresh token in the OS credential vault, never config.json."""
    refresh = account.get("refresh_token")
    uuid = account.get("uuid")
    client_id = account.get("client_id")
    if not refresh or not uuid or not client_id:
        return False
    try:
        import keyring
        keyring.set_password(ACCOUNT_KEYRING_SERVICE, _account_key(uuid, "refresh_token"), refresh)
        keyring.set_password(ACCOUNT_KEYRING_SERVICE, _account_key(uuid, "client_id"), client_id)
        current_accounts = cfg.get("ms_accounts", [])
        if not isinstance(current_accounts, list):
            current_accounts = []
        accounts = [a for a in current_accounts
                    if isinstance(a, dict) and a.get("uuid") != uuid]
        accounts.append({"uuid": uuid, "username": account.get("username", "Minecraft account"),
                         "client_id": client_id})
        cfg["ms_accounts"] = accounts
        cfg["selected_ms_account"] = uuid
        save_cfg(cfg)
        return True
    except Exception as ex:
        debug_log(f"Не удалось сохранить Microsoft refresh token в системном хранилище: {ex}")
        return False


def get_saved_microsoft_accounts():
    accounts = cfg.get("ms_accounts", [])
    if not isinstance(accounts, list):
        return []
    return [a for a in accounts if isinstance(a, dict) and a.get("uuid")]


def remove_saved_microsoft_account(uuid):
    """Remove one remembered account from keyring and the local account index."""
    try:
        import keyring
        for field in ("refresh_token", "client_id"):
            try:
                keyring.delete_password(ACCOUNT_KEYRING_SERVICE, _account_key(uuid, field))
            except Exception:
                pass
        # Backward compatibility with the previous single-account storage keys.
        remaining = [a for a in get_saved_microsoft_accounts() if a.get("uuid") != uuid]
        if not remaining:
            for field in ("refresh_token", "client_id"):
                try:
                    keyring.delete_password(ACCOUNT_KEYRING_SERVICE, field)
                except Exception:
                    pass
    except Exception:
        pass
    cfg["ms_accounts"] = [a for a in get_saved_microsoft_accounts() if a.get("uuid") != uuid]
    if cfg.get("selected_ms_account") == uuid:
        cfg["selected_ms_account"] = ""
    save_cfg(cfg)


def clear_saved_account():
    """Legacy helper: remove all remembered Microsoft credentials."""
    for account in list(get_saved_microsoft_accounts()):
        remove_saved_microsoft_account(account.get("uuid"))
    try:
        import keyring
        for key in ("refresh_token", "client_id"):
            try:
                keyring.delete_password(ACCOUNT_KEYRING_SERVICE, key)
            except Exception:
                pass
    except Exception:
        pass
    cfg["ms_accounts"] = []
    cfg["selected_ms_account"] = ""
    save_cfg(cfg)


def launch(version, nickname, mem_mb, on_output=None, on_exit=None, account=None):
    vdir = os.path.join(VERSIONS_DIR, version)
    # These folders are intentionally version-local (not shared in ./minecraft).
    for folder in ("mods", "config", "saves", "resourcepacks", "shaderpacks",
                   "screenshots", "logs", "crash-reports", "datapacks"):
        os.makedirs(os.path.join(vdir, folder), exist_ok=True)
    cmd, n_native_files, java_exe, ignored = build_launch_cmd(version, nickname, mem_mb, vdir, account=account)
    if on_output:
        on_output(f"Java: {java_exe}")
        on_output(f"[native] Извлечено нативных файлов: {n_native_files} -> {os.path.join(vdir, 'natives')}")
        if n_native_files == 0:
            on_output("[native] ВНИМАНИЕ: 0 нативов! Запусти «Играть» ещё раз.")
        if ignored:
            on_output(f"[libs] Игнорируются отсутствующие библиотеки ({len(ignored)} шт.): "
                      + ", ".join(ignored[:10]) + ("..." if len(ignored) > 10 else ""))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=vdir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=flags,
                            text=True, errors="replace", bufsize=1)
    def reader():
        for line in proc.stdout:
            if on_output:
                on_output(line.rstrip())
        proc.wait()
        if on_exit:
            on_exit(proc.returncode)
    threading.Thread(target=reader, daemon=True).start()
    return proc

# ---------- GUI ----------
import tkinter as tk
from tkinter import messagebox, filedialog, colorchooser

cfg = load_cfg()
os.makedirs(VERSIONS_DIR, exist_ok=True)
os.makedirs(JAVA_DIR, exist_ok=True)
ensure_launcher_profiles()

PALETTES = {
    "light": dict(bg="#f0f0f0", fg="#111111", accent="#e0e0e0", log_bg="#ffffff",
                  installed="#111111", notinstalled="#888888"),
    "dark":  dict(bg="#1e1e28", fg="#e8e8f0", accent="#2c2c3a", log_bg="#12121a",
                  installed="#e8e8f0", notinstalled="#777788"),
}
pal = dict(PALETTES["dark"] if cfg["dark_theme"] else PALETTES["light"])

if cfg.get("text_color"):
    pal["fg"] = pal["installed"] = cfg["text_color"]
if cfg.get("bg_color"):
    pal["bg"] = cfg["bg_color"]
if cfg.get("accent_color"):
    pal["accent"] = cfg["accent_color"]

root = tk.Tk()
root.title("hjiky лаунчер майнкрафт")
root.geometry("480x495")

def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return None

def apply_titlebar_color(win, hex_color, dark_text=None):
    if os.name != "nt":
        return
    try:
        import ctypes
        rgb = _hex_to_rgb(hex_color)
        if rgb is None:
            return
        color = ctypes.c_uint(rgb[0] | (rgb[1] << 8) | (rgb[2] << 16))
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
        if dark_text is not None:
            t = ctypes.c_uint(0x00000000 if dark_text else 0x00FFFFFF)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 36, ctypes.byref(t), ctypes.sizeof(t))
    except Exception:
        pass

def update_titlebar():
    color = pal["accent"] if not os.path.exists(THEME_IMG) else "#1e1e28"
    rgb = _hex_to_rgb(color)
    dark_text = (sum(rgb) / 3 > 128) if rgb else True
    apply_titlebar_color(root, color, dark_text)

styled = []

def S(widget, role="bg", **kw):
    role_pal = {"bg": pal["bg"], "accent": pal["accent"], "log": pal["log_bg"]}
    if "bg" not in kw:
        kw["bg"] = role_pal.get(role, pal["bg"])
    try:
        widget.configure(**kw)
    except Exception:
        pass
    styled.append((widget, role, kw))
    return widget

def restyle():
    for item in list(styled):
        w, role, kw = item
        try:
            if not w.winfo_exists():
                styled.remove(item)
                continue
        except Exception:
            continue
        kw2 = dict(kw)
        kw2["bg"] = {"bg": pal["bg"], "accent": pal["accent"], "log": pal["log_bg"]}.get(role, pal["bg"])
        if "fg" in kw2:
            kw2["fg"] = pal["fg"]
        if "disabledforeground" in kw2:
            kw2["disabledforeground"] = pal["notinstalled"]
        if "insertbackground" in kw2:
            kw2["insertbackground"] = pal["fg"]
        if "selectbackground" in kw2:
            kw2["selectbackground"] = "#3a6ea5"
        if "selectcolor" in kw2:
            kw2["selectcolor"] = pal["accent"]
        if "activebackground" in kw2:
            kw2["activebackground"] = kw2["bg"]
        if "activeforeground" in kw2:
            kw2["activeforeground"] = pal["fg"]
        if "troughcolor" in kw2:
            kw2["troughcolor"] = pal["bg"]
        try:
            w.configure(**kw2)
        except Exception:
            pass
    root.configure(bg=pal["bg"])
    update_titlebar()
    rebuild_list()

log_win = None
log_text = None
dbg_win = None
dbg_text = None

def _make_text_window(title, size, existing_state, role="log"):
    win, text = existing_state
    if win is not None and win.winfo_exists():
        win.lift(); win.focus_force()
        return win, text
    win = S(tk.Toplevel(root), role="bg")
    win.title(title)
    win.geometry(size)
    # Панель действий для окна отладки: весь накопленный вывод копируется одной кнопкой.
    if role == "debug":
        toolbar = S(tk.Frame(win), role="bg")
        toolbar.pack(side="top", fill="x", padx=6, pady=5)
        copy_btn = S(tk.Button(toolbar, text="📋 Копировать весь лог",
                               command=lambda: copy_debug_log(text, copy_btn)),
                     role="accent", fg=pal["fg"])
        copy_btn.pack(side="left")
        S(tk.Label(toolbar, text="Копирует весь текст из этой консоли"),
          role="bg", fg=pal["notinstalled"]).pack(side="left", padx=8)
    text = S(tk.Text(win, wrap="word", state="disabled", font=("Consolas", 9)),
             role=role, fg=pal["fg"], insertbackground=pal["fg"])
    sb = S(tk.Scrollbar(win, command=text.yview), role="bg", troughcolor=pal["bg"],
           activebackground=pal["accent"])
    text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    text.pack(fill="both", expand=True)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    color = pal["accent"] if not os.path.exists(THEME_IMG) else "#1e1e28"
    rgb = _hex_to_rgb(color)
    dark_text = (sum(rgb) / 3 > 128) if rgb else True
    win.after(50, lambda: apply_titlebar_color(win, color, dark_text))
    return win, text

def copy_debug_log(text_widget, button=None):
    """Копирует весь текущий текст консоли отладки в системный буфер обмена."""
    try:
        content = text_widget.get("1.0", "end-1c") if text_widget is not None else ""
        if not content.strip():
            content = "(консоль отладки пока пуста)"
        root.clipboard_clear()
        root.clipboard_append(content)
        root.update_idletasks()  # гарантируем передачу данных системному буферу
        if button is not None:
            try:
                button.configure(text="✓ Лог скопирован")
                button.after(1400, lambda: button.winfo_exists() and
                             button.configure(text="📋 Копировать весь лог"))
            except Exception:
                pass
    except Exception as ex:
        messagebox.showerror("Копирование лога", f"Не удалось скопировать лог:\n{ex}")


def append_text(text_widget, msg):
    if text_widget is None:
        return
    try:
        text_widget.configure(state="normal")
        text_widget.insert("end", msg + "\n")
        text_widget.see("end")
        text_widget.configure(state="disabled")
    except Exception:
        pass

def log(msg, level="INFO"):
    if level == "ERROR" and not cfg.get("errors_in_window"):
        messagebox.showerror("Ошибка", msg)
        return
    global log_win, log_text
    log_win, log_text = _make_text_window("Журнал", "560x320", (log_win, log_text))
    append_text(log_text, f"[{level}] {msg}")


def log_device_auth_instruction(message):
    """Show the device-login URL as a link and the one-time code as copyable text."""
    global log_win, log_text
    log_win, log_text = _make_text_window("Журнал", "560x320", (log_win, log_text))
    if log_text is None:
        return
    match = re.search(r"(https?://\S+)\s+и введи код\s+([A-Z0-9-]+)", message, re.I)
    if not match:
        append_text(log_text, f"[INFO] {message}")
        return
    url, code = match.group(1).rstrip(".,)"), match.group(2)
    try:
        log_text.configure(state="normal")
        log_text.insert("end", "[INFO] Открой ")
        start = log_text.index("end-1c")
        log_text.insert("end", url)
        end = log_text.index("end-1c")
        log_text.tag_add("device_login_url", start, end)
        log_text.insert("end", " и введи код ")
        code_start = log_text.index("end-1c")
        log_text.insert("end", code)
        code_end = log_text.index("end-1c")
        log_text.tag_add("device_login_code", code_start, code_end)
        log_text.insert("end", "\n")
        log_text.tag_configure("device_login_url", foreground="#1683e8", underline=True)
        log_text.tag_configure("device_login_code", foreground="#16803c", underline=True)
        log_text.tag_bind("device_login_url", "<Button-1>",
                          lambda _event, target=url: webbrowser.open(target))
        log_text.tag_bind("device_login_url", "<Enter>",
                          lambda _event: log_text.configure(cursor="hand2"))
        log_text.tag_bind("device_login_url", "<Leave>",
                          lambda _event: log_text.configure(cursor="xterm"))
        def copy_code(_event, value=code):
            root.clipboard_clear()
            root.clipboard_append(value)
            root.update_idletasks()
            status.config(text="Код Microsoft скопирован в буфер обмена")
            return "break"
        log_text.tag_bind("device_login_code", "<Button-1>", copy_code)
        log_text.tag_bind("device_login_code", "<Enter>",
                          lambda _event: log_text.configure(cursor="hand2"))
        log_text.tag_bind("device_login_code", "<Leave>",
                          lambda _event: log_text.configure(cursor="xterm"))
        log_text.see("end")
        log_text.configure(state="disabled")
    except Exception as ex:
        try:
            log_text.configure(state="disabled")
        except Exception:
            pass
        debug_log(f"Не удалось оформить ссылку/код входа: {ex}")

def debug_log(msg):
    # Tk widgets must only be touched from the GUI thread.
    if threading.current_thread() is not threading.main_thread():
        try:
            root.after(0, lambda m=str(msg): debug_log(m))
        except Exception:
            pass
        return
    if not cfg.get("debug_console", True):
        return
    global dbg_win, dbg_text
    dbg_win, dbg_text = _make_text_window("Консоль отладки", "700x420", (dbg_win, dbg_text), role="debug")
    append_text(dbg_text, msg)

def error(msg):
    log(msg, level="ERROR")

# ---------- фон-фото ----------
bg_label = None
photo_ref = None
_last_size = [0, 0]
_resize_job = None

def apply_bg_photo(force=False):
    global bg_label, photo_ref
    if not os.path.exists(THEME_IMG):
        if bg_label is not None:
            bg_label.destroy()
            bg_label = None
            photo_ref = None
        return
    try:
        from PIL import Image, ImageTk
        w = max(root.winfo_width(), 1)
        h = max(root.winfo_height(), 1)
        if not force and (w, h) == tuple(_last_size) and bg_label is not None:
            return
        _last_size[0], _last_size[1] = w, h
        img = Image.open(THEME_IMG).convert("RGB")
        sw, sh = img.size
        scale = max(w / sw, h / sh)
        nw, nh = max(w, int(sw * scale)), max(h, int(sh * scale))
        img = img.resize((nw, nh), Image.LANCZOS)
        left = (nw - w) // 2
        top = (nh - h) // 2
        img = img.crop((left, top, left + w, top + h))
        photo_ref = ImageTk.PhotoImage(img)
        if bg_label is None:
            bg_label = tk.Label(root, image=photo_ref, borderwidth=0)
            bg_label.place(x=0, y=0, relwidth=1, relheight=1)
            bg_label.lower()
        else:
            bg_label.configure(image=photo_ref)
    except Exception as ex:
        error(f"Не удалось применить фото-тему: {ex}")

def apply_theme():
    root.configure(bg=pal["bg"])
    restyle()
    apply_bg_photo(force=True)
    update_titlebar()

# ---------- список версий ----------
frm = tk.Frame(root)
S(frm, role="bg")
frm.pack(fill="both", expand=True, padx=10, pady=10)

remote_files = []
vanilla_catalog = []
fabric_catalog = []
forge_catalog = {}
neoforge_catalog = []
installing = False
preparing = False

def version_display_list():
    installed = installed_versions()
    items = [(v, v, "installed", None) for v in installed]
    known = set(installed)
    if cfg.get("show_vanilla", True):
        for v in vanilla_catalog:
            vid = v["id"]
            if vid not in known:
                items.append((f"{vid}   ◦ Mojang ({v.get('type','release')})",
                              vid, "vanilla", None))
                known.add(vid)
    if cfg.get("show_fabric", True):
        for mc, _loader in fabric_catalog:
            vid = "fabric-" + mc
            if mc not in known and vid not in known:
                items.append((f"{mc}   ✦ Fabric", vid, "fabric", None))
                known.add(mc)
    if cfg.get("show_forge", True):
        for mc in forge_catalog:
            vid = "forge-" + mc
            if mc not in known and vid not in known:
                items.append((f"{mc}   ⛨ Forge", vid, "forge", None))
                known.add(mc)
    if cfg.get("show_neoforge", True):
        for mc in neoforge_catalog:
            vid = "neoforge-" + mc
            if mc not in known and vid not in known:
                items.append((f"{mc}   ⛨ NeoForge", vid, "neoforge", None))
                known.add(mc)
    if cfg.get("gdrive_link"):
        for name, fid in remote_files:
            base = os.path.splitext(name)[0]
            if base not in known:
                items.append((f"{base}   ● сборка (Drive)", base, "drive", fid))
                known.add(base)
    return items

listbox = tk.Listbox(frm, exportselection=False, height=8, activestyle="none")
S(listbox, role="accent", fg=pal["fg"], selectbackground="#3a6ea5")
listbox.pack(fill="both", expand=True, pady=4)

def rebuild_list(keep=None):
    items = version_display_list()
    sel = keep or sel_version()
    listbox.delete(0, "end")
    for i, (text, real, kind, fid) in enumerate(items):
        listbox.insert("end", text)
        fg = pal["installed"] if kind == "installed" else pal["notinstalled"]
        listbox.itemconfig(i, foreground=fg, background=pal["accent"],
                           selectforeground="#ffffff")
        if real == sel:
            listbox.selection_clear(0, "end")
            listbox.selection_set(i)
            listbox.see(i)

def sel_version():
    sels = listbox.curselection()
    if not sels:
        return None
    return version_display_list()[sels[0]][1]

def sel_entry():
    sels = listbox.curselection()
    if not sels:
        return None
    return version_display_list()[sels[0]]

status = tk.Label(frm, anchor="w")
S(status, role="bg", fg=pal["fg"])
status.pack(fill="x", pady=2)

# Общий индикатор установки: текущий файл, объём и процент.
progress_text = tk.Label(frm, text="Ожидание загрузки", anchor="w")
S(progress_text, role="bg", fg=pal["fg"])
progress_text.pack(fill="x", pady=(2, 0))
file_count_text = tk.Label(frm, text="Файлы: 0 загружено · ожидают подсчёта", anchor="w")
S(file_count_text, role="bg", fg=pal["fg"])
file_count_text.pack(fill="x", pady=(0, 2))
try:
    from tkinter import ttk
    progress_bar = ttk.Progressbar(frm, orient="horizontal", mode="determinate", maximum=100)
    progress_bar.pack(fill="x", pady=(0, 5))
except Exception:
    progress_bar = None

# Счётчик файлов и оценка времени обновляются на каждом событии загрузки.
_progress_stats = {"planned": 0, "done": 0, "started": None, "rate": 0.0, "last_bytes": 0, "last_time": None, "pending": 0}

def reset_progress(message=""):
    _progress_stats.update({"planned": 0, "done": 0, "started": None, "rate": 0.0, "last_bytes": 0, "last_time": None, "pending": 0})
    def apply():
        if progress_bar is not None:
            progress_bar.stop()
            progress_bar.configure(mode="determinate", value=0)
        progress_text.config(text=message)
        if 'file_count_text' in globals():
            file_count_text.config(text="Файлы: 0 загружено · ожидают подсчёта")
    root.after(0, apply)

def ui_progress(event):
    if isinstance(event, dict):
        if event.get("type") == "file_plan":
            planned_now = int(event.get("count", 0))
            _progress_stats["planned"] += planned_now
            _progress_stats["pending"] += planned_now
            count_label = f"Файлы: {_progress_stats['done']} загружено · {_progress_stats['pending']} осталось"
            root.after(0, lambda t=count_label: file_count_text.config(text=t))
            return
        if event.get("type") == "download_done":
            if _progress_stats["pending"] > 0:
                _progress_stats["done"] += 1
                _progress_stats["pending"] -= 1
            count_label = f"Файлы: {_progress_stats['done']} загружено · {_progress_stats['pending']} осталось" if _progress_stats["planned"] else f"Файлы: {_progress_stats['done']} загружено"
            root.after(0, lambda t=count_label: file_count_text.config(text=t))
        if event.get("type") == "download":
            now = time.monotonic()
            prev_t, prev_b = _progress_stats.get("last_time"), _progress_stats.get("last_bytes", 0)
            current_b = int(event.get("downloaded", 0))
            if prev_t is not None and now > prev_t and current_b >= prev_b:
                instant = (current_b - prev_b) / (now - prev_t)
                _progress_stats["rate"] = instant if not _progress_stats["rate"] else 0.25 * instant + 0.75 * _progress_stats["rate"]
            _progress_stats["last_time"], _progress_stats["last_bytes"] = now, current_b
        if event.get("type") == "download_done":
            _progress_stats["last_time"], _progress_stats["last_bytes"] = None, 0
            _progress_stats["rate"] = 0.0
        downloaded = int(event.get("downloaded", 0))
        total = int(event.get("total", 0) or 0)
        pct = event.get("percent")
        filename = event.get("name") or os.path.basename(urllib.parse.urlparse(event.get("url", "")).path) or "файл"
        action = "Распаковка" if event.get("type") == "extract" else "Загрузка"
        if pct is None:
            label = f"{action} {filename}: {downloaded / 1048576:.1f} МБ"
        else:
            pct = max(0, min(100, float(pct)))
            if event.get("type") == "extract":
                label = f"Распаковка файлов версии: {pct:.1f}% ({event.get('index', 0)}/{event.get('count', 0)} файлов)"
            else:
                label = f"Загрузка {filename}: {pct:.1f}% ({downloaded / 1048576:.1f} / {total / 1048576:.1f} МБ)"
                rate = _progress_stats.get("rate", 0.0)
                if total > downloaded and rate > 1024:
                    secs = int((total - downloaded) / rate)
                    eta = f"{secs // 60} мин {secs % 60} сек" if secs >= 60 else f"{secs} сек"
                    label += f" · осталось примерно {eta}"
        def apply():
            progress_text.config(text=label)
            status.config(text=label)
            if progress_bar is not None:
                if pct is None:
                    progress_bar.configure(mode="indeterminate")
                    progress_bar.start(10)
                else:
                    progress_bar.stop()
                    progress_bar.configure(mode="determinate", value=pct)
        root.after(0, apply)
    else:
        msg = str(event)
        def apply_msg():
            progress_text.config(text=msg)
            status.config(text=msg)
        root.after(0, apply_msg)

def on_list_dblclick(event):
    entry = sel_entry()
    if not entry:
        return
    text, real, kind, fid = entry
    if kind == "vanilla":
        install_vanilla_async(real)
    elif kind == "fabric":
        install_fabric_async(real)
    elif kind == "forge":
        install_forge_async(real)
    elif kind == "neoforge":
        install_neoforge_async(real)
    elif kind == "drive" and cfg.get("gdrive_link"):
        install_selected(real)
listbox.bind("<Double-Button-1>", on_list_dblclick)

def install_selected(name=None):
    global installing
    if installing:
        return
    if not cfg.get("gdrive_link"):
        error("Сначала укажи ссылку на папку Google Drive в настройках")
        return
    if name is None:
        entry = sel_entry()
        if not entry:
            error("Выбери версию в списке")
            return
        _, name, kind, fid = entry
        if kind == "installed":
            status.config(text=f"«{name}» уже установлена")
            return
    fid = next((f for n, f in remote_files if os.path.splitext(n)[0] == name or n == name), None)
    if not fid:
        error(f"«{name}» не найдена в папке Drive — обнови список")
        return
    installing = True
    status.config(text=f"Скачиваю {name}...")
    def worker():
        global installing
        try:
            tmp = os.path.join(tempfile.gettempdir(), "mc_dl")
            os.makedirs(tmp, exist_ok=True)
            zp = os.path.join(tmp, name + ".zip")
            ui_progress("Скачиваю архив версии из Google Drive...")
            download_file(fid, zp)
            ui_progress("Распаковываю и устанавливаю файлы версии...")
            install_version(zp, on_progress=ui_progress)
            root.after(0, lambda: (log(f"Установлена версия: {name}"),
                                   rebuild_list(name),
                                   status.config(text=f"Установлена: {name}"), progress_text.config(text="Установка завершена"), progress_bar.configure(value=100) if progress_bar is not None else None))
        except Exception as ex:
            err_text = str(ex)
            root.after(0, lambda: (error(f"Ошибка установки {name}: {err_text}"),
                                   status.config(text="")))
        finally:
            installing = False
    threading.Thread(target=worker, daemon=True).start()

def install_vanilla_async(vid):
    global installing
    if installing:
        return
    installing = True
    status.config(text=f"Устанавливаю Mojang {vid}...")
    def prog(msg):
        root.after(0, lambda m=msg: (debug_log(m), ui_progress(m)))
    def worker():
        global installing
        try:
            install_vanilla(vid, on_progress=prog)
            root.after(0, lambda: (log(f"Установлена Mojang-версия: {vid}"),
                                   rebuild_list(vid),
                                   status.config(text=f"Установлена: {vid}"), progress_text.config(text="Установка завершена"), progress_bar.configure(value=100) if progress_bar is not None else None))
        except Exception as ex:
            err_text = str(ex)
            root.after(0, lambda: (error(f"Ошибка установки {vid}: {err_text}"),
                                   debug_log("ОШИБКА УСТАНОВКИ:\n" + err_text),
                                   status.config(text="")))
        finally:
            installing = False
    threading.Thread(target=worker, daemon=True).start()

def install_fabric_async(mc_ver):
    global installing
    if installing:
        return
    installing = True
    status.config(text=f"Устанавливаю Fabric {mc_ver}...")
    def worker():
        global installing
        try:
            prog = lambda m: root.after(0, lambda mm=m: (debug_log(mm), ui_progress(mm)))
            vid = install_fabric(mc_ver, on_progress=prog)
            root.after(0, lambda: (log(f"Установлена: {vid}"),
                                   rebuild_list(vid),
                                   status.config(text=f"Установлена: {vid}"), progress_text.config(text="Установка завершена"), progress_bar.configure(value=100) if progress_bar is not None else None))
        except Exception as ex:
            e = str(ex)
            root.after(0, lambda: (error(f"Ошибка Fabric {mc_ver}: {e}"),
                                   debug_log("ОШИБКА:\n" + e),
                                   status.config(text="")))
        finally:
            installing = False
    threading.Thread(target=worker, daemon=True).start()

def install_forge_async(mc_ver):
    global installing
    if installing:
        return
    installing = True
    status.config(text=f"Устанавливаю Forge {mc_ver}...")
    def worker():
        global installing
        try:
            prog = lambda m: root.after(0, lambda mm=m: (debug_log(mm), ui_progress(mm)))
            vid = install_forge(mc_ver, on_progress=prog)
            root.after(0, lambda: (log(f"Установлена: {vid}"),
                                   rebuild_list(vid),
                                   status.config(text=f"Установлена: {vid}"), progress_text.config(text="Установка завершена"), progress_bar.configure(value=100) if progress_bar is not None else None))
        except Exception as ex:
            e = str(ex)
            root.after(0, lambda: (error(f"Ошибка Forge {mc_ver}: {e}"),
                                   debug_log("ОШИБКА:\n" + e),
                                   status.config(text="")))
        finally:
            installing = False
    threading.Thread(target=worker, daemon=True).start()

def install_neoforge_async(mc_ver):
    if mc_ver.startswith("neoforge-"):
        mc_ver = mc_ver[len("neoforge-"):]
    global installing
    if installing:
        return
    installing = True
    status.config(text=f"Устанавливаю NeoForge для Minecraft {mc_ver}...")
    def worker():
        global installing
        try:
            prog = lambda m: root.after(0, lambda mm=m: (debug_log(mm), ui_progress(mm)))
            vid = install_neoforge(mc_ver, on_progress=prog)
            root.after(0, lambda: (log(f"Установлена: {vid}"), rebuild_list(vid),
                                   status.config(text=f"Установлена: {vid}"), progress_text.config(text="Установка завершена"), progress_bar.configure(value=100) if progress_bar is not None else None))
        except Exception as ex:
            msg = str(ex)
            root.after(0, lambda: (error(f"Ошибка NeoForge: {msg}"), status.config(text="")))
        finally:
            installing = False
    threading.Thread(target=worker, daemon=True).start()


def install_selected_or_vanilla():
    entry = sel_entry()
    if not entry:
        error("Выбери версию в списке")
        return
    text, real, kind, fid = entry
    if kind == "installed":
        status.config(text=f"«{real}» уже установлена")
        return
    if kind == "vanilla":
        install_vanilla_async(real)
    elif kind == "fabric":
        install_fabric_async(real)
    elif kind == "forge":
        install_forge_async(real)
    elif kind == "neoforge":
        install_neoforge_async(real)
    elif kind == "drive":
        install_selected(real)

def refresh_vanilla_async(silent=False):
    def worker():
        try:
            versions = fetch_version_manifest(
                on_status=lambda u: root.after(0, lambda: debug_log(u)))
            result = [(v["id"], v.get("type", "release"))
                      for v in versions
                      if v.get("type") in ("release", "snapshot")]
            root.after(0, lambda: (set_vanilla(result),
                                   status.config(text=f"В каталоге Mojang: {len(result)} версий"),
                                   refresh_mod_loaders_async()))
        except Exception as ex:
            err_text = str(ex)
            if silent:
                root.after(0, lambda: debug_log(f"Каталог Mojang недоступен: {err_text}"))
            else:
                root.after(0, lambda: (error(f"Ошибка каталога Mojang: {err_text}"),
                                       status.config(text="")))
    threading.Thread(target=worker, daemon=True).start()

def set_vanilla(items):
    global vanilla_catalog
    vanilla_catalog = [{"id": i, "type": t} for i, t in items]
    rebuild_list()

def refresh_mod_loaders_async():
    def worker():
        global fabric_catalog, forge_catalog, neoforge_catalog
        try:
            fab = []
            if cfg.get("show_fabric", True):
                try:
                    fab = fetch_fabric_versions()
                except Exception as ex:
                    debug_log(f"Fabric-каталог недоступен: {ex}")
            forge = {}
            neo = []
            if cfg.get("show_neoforge", True):
                for v in vanilla_catalog:
                    if v["type"] != "release":
                        continue
                    try:
                        if fetch_neoforge_versions(v["id"]):
                            neo.append(v["id"])
                    except Exception:
                        pass
            if cfg.get("show_forge", True):
                for v in vanilla_catalog:
                    if v["type"] != "release":
                        continue
                    try:
                        forge[v["id"]] = fetch_forge_versions(v["id"])
                    except Exception:
                        pass
            def apply():
                global fabric_catalog, forge_catalog, neoforge_catalog
                fabric_catalog, forge_catalog, neoforge_catalog = fab, forge, neo
                rebuild_list()
                status.config(text=f"Fabric: {len(fab)} | Forge: {len(forge)} | NeoForge: {len(neo)} версий")
            root.after(0, apply)
        except Exception as ex:
            e = str(ex)
            root.after(0, lambda: debug_log(f"Ошибка загрузки Fabric/Forge: {e}"))
    threading.Thread(target=worker, daemon=True).start()

def refresh_remote_async(silent=False):
    if not cfg.get("gdrive_link"):
        if not silent:
            error("Ссылка на папку Drive не задана")
        return
    status.config(text="Обновляю каталог с Drive...")
    def worker():
        try:
            files = list_folder_zips(cfg["gdrive_link"])
            root.after(0, lambda: (set_remote(files),
                                   status.config(text=f"Найдено в Drive: {len(files)}")))
        except Exception as ex:
            err_text = str(ex)
            if not silent:
                root.after(0, lambda: (error(f"Ошибка Drive: {err_text}"), status.config(text="")))
    threading.Thread(target=worker, daemon=True).start()

def set_remote(files):
    global remote_files
    remote_files = files
    log(f"В папке Drive найдено ZIP: {len(files)}")
    rebuild_list()

def update_status():
    mb = cfg.get("memory_mb", 2048)
    mem_txt = f"{mb/1024:g} ГБ" if mb % 1024 == 0 else f"{mb} МБ"
    mode = (f"Microsoft: {active_account['username']}" if active_account
            else f"Офлайн: {cfg['nickname']}")
    status.config(text=f"{mode} | Память: {mem_txt} | Java: {cfg.get('java','Авто')}")


account_manager_refresh_callback = None


def _set_active_microsoft_account(account, saved=True, log_message=None):
    global active_account
    active_account = {k: v for k, v in account.items() if k != "refresh_token"}
    cfg["nickname"] = account.get("username", cfg.get("nickname", "Player"))
    cfg["selected_ms_account"] = account.get("uuid", "")
    save_cfg(cfg)
    status.config(text=f"Microsoft: {account.get('username', 'Minecraft account')}" +
                  ("" if saved else " (сеанс только до закрытия)"))
    update_status()
    if log_message:
        log(log_message)
    if account_manager_refresh_callback:
        try:
            account_manager_refresh_callback()
        except Exception:
            pass


def microsoft_login():
    """Add another Microsoft account using browser sign-in and account picker."""
    client_id = cfg.get("ms_client_id", "").strip()
    if not client_id:
        error("Укажи Microsoft Client ID в настройках. Это публичный ID приложения, не пароль.")
        return
    status.config(text="Открываю браузер для входа Microsoft…")

    def worker():
        try:
            account = microsoft_browser_login(
                client_id, on_status=lambda m: root.after(0, lambda msg=m: status.config(text=msg)))
            saved = save_account_refresh_token(account) if account.get("refresh_token") else False
            def finish():
                _set_active_microsoft_account(
                    account, saved=saved,
                    log_message=f"Microsoft-вход выполнен: {account['username']}; лицензия Java Edition подтверждена.")
            root.after(0, finish)
        except Exception as ex:
            msg = str(ex)
            root.after(0, lambda: (error("Ошибка входа Microsoft: " + msg),
                                   status.config(text="Вход не выполнен")))
    threading.Thread(target=worker, daemon=True).start()




def microsoft_device_login_ui():
    """Add a licensed Microsoft account using the device-code flow."""
    client_id = cfg.get("ms_client_id", "").strip()
    if not client_id:
        error("Укажи Microsoft Client ID в настройках. Это публичный ID приложения, не пароль.")
        return
    status.config(text="Подготавливаю вход по коду Microsoft…")

    def report(message):
        def show():
            status.config(text=message)
            log_device_auth_instruction(message)
        root.after(0, show)

    def worker():
        try:
            account = microsoft_device_login(client_id, on_status=report)
            saved = save_account_refresh_token(account) if account.get("refresh_token") else False
            root.after(0, lambda: _set_active_microsoft_account(
                account, saved=saved,
                log_message=f"Вход по коду Microsoft выполнен: {account['username']}; лицензия Java Edition подтверждена."))
        except Exception as ex:
            msg = str(ex)
            root.after(0, lambda: (error("Ошибка входа по коду Microsoft: " + msg),
                                   status.config(text="Вход не выполнен")))
    threading.Thread(target=worker, daemon=True).start()


def switch_microsoft_account_async(uuid):
    """Activate a previously remembered Microsoft account by refreshing its token."""
    meta = next((a for a in get_saved_microsoft_accounts() if a.get("uuid") == uuid), None)
    if not meta:
        error("Сохранённый аккаунт не найден. Добавь его через Microsoft-вход.")
        return
    status.config(text=f"Переключаюсь на {meta.get('username', 'Microsoft account')}…")

    def worker():
        try:
            import keyring
            refresh = keyring.get_password(ACCOUNT_KEYRING_SERVICE, _account_key(uuid, "refresh_token"))
            client_id = keyring.get_password(ACCOUNT_KEYRING_SERVICE, _account_key(uuid, "client_id")) or meta.get("client_id")
            if not refresh or not client_id:
                raise RuntimeError("Для этого аккаунта нет сохранённой сессии. Войди в него заново.")
            account = microsoft_refresh_login(client_id, refresh)
            saved = save_account_refresh_token(account) if account.get("refresh_token") else False
            root.after(0, lambda: _set_active_microsoft_account(
                account, saved=saved,
                log_message=f"Выбран Microsoft-аккаунт: {account['username']}"))
        except Exception as ex:
            msg = str(ex)
            root.after(0, lambda: (error("Не удалось переключить аккаунт: " + msg), update_status()))
    threading.Thread(target=worker, daemon=True).start()


def open_microsoft_accounts():
    """Small account manager: switch, add or forget remembered Microsoft accounts."""
    global account_manager_refresh_callback
    win = tk.Toplevel(root)
    S(win, role="bg")
    win.title("Microsoft-аккаунты")
    win.geometry("520x370")
    win.transient(root)
    win.grab_set()
    outer = tk.Frame(win); S(outer, role="bg"); outer.pack(fill="both", expand=True, padx=12, pady=12)
    S(tk.Label(outer, text="Сохранённые аккаунты Minecraft Java Edition"), role="bg", fg=pal["fg"]).pack(anchor="w")
    account_list = tk.Listbox(outer, height=9, exportselection=False)
    S(account_list, role="accent", fg=pal["fg"]).pack(fill="both", expand=True, pady=8)
    listed = []

    def refresh_list():
        listed.clear()
        account_list.delete(0, "end")
        accounts = get_saved_microsoft_accounts()
        selected = cfg.get("selected_ms_account", "")
        for item in accounts:
            listed.append(item)
            marker = "✓ " if item.get("uuid") == selected else "  "
            account_list.insert("end", marker + item.get("username", "Minecraft account"))
        if not accounts:
            account_list.insert("end", "Нет сохранённых аккаунтов")

    def selected_meta():
        sel = account_list.curselection()
        if not sel or sel[0] >= len(listed):
            return None
        return listed[sel[0]]

    def use_selected():
        item = selected_meta()
        if item:
            switch_microsoft_account_async(item["uuid"])

    def remove_selected():
        item = selected_meta()
        if not item:
            return
        if not messagebox.askyesno("Удалить аккаунт", f"Удалить сохранённый аккаунт {item.get('username')} из лаунчера?\nСама лицензия Microsoft не затрагивается.", parent=win):
            return
        global active_account
        if active_account and active_account.get("uuid") == item.get("uuid"):
            active_account = None
        remove_saved_microsoft_account(item["uuid"])
        refresh_list()
        update_status()

    row = tk.Frame(outer); S(row, role="bg"); row.pack(fill="x")
    S(tk.Button(row, text="Использовать", command=use_selected), role="accent", fg=pal["fg"]).pack(side="left", padx=2)
    S(tk.Button(row, text="Добавить через браузер", command=microsoft_login), role="accent", fg=pal["fg"]).pack(side="left", padx=2)
    S(tk.Button(row, text="Войти по коду", command=microsoft_device_login_ui), role="accent", fg=pal["fg"]).pack(side="left", padx=2)
    S(tk.Button(row, text="Удалить", command=remove_selected), role="accent", fg=pal["fg"]).pack(side="left", padx=2)
    S(tk.Button(outer, text="Закрыть", command=win.destroy), role="accent", fg=pal["fg"]).pack(anchor="e", pady=(8, 0))

    account_manager_refresh_callback = refresh_list
    def close_manager():
        global account_manager_refresh_callback
        account_manager_refresh_callback = None
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", close_manager)
    refresh_list()


def microsoft_logout():
    """Sign out of the current session but keep other remembered accounts available."""
    global active_account
    active_account = None
    cfg["selected_ms_account"] = ""
    save_cfg(cfg)
    status.config(text="Выход из текущего Microsoft-сеанса (сохранённые аккаунты оставлены)")
    update_status()
    log("Текущий Microsoft-сеанс отключён. Сохранённые аккаунты можно выбрать в менеджере.")


def restore_microsoft_account_async():
    """Automatically restore the last selected Microsoft session from OS keyring."""
    def worker():
        try:
            import keyring
            accounts = get_saved_microsoft_accounts()
            selected_uuid = cfg.get("selected_ms_account", "")
            meta = next((a for a in accounts if a.get("uuid") == selected_uuid), None)
            if meta:
                refresh = keyring.get_password(ACCOUNT_KEYRING_SERVICE, _account_key(meta["uuid"], "refresh_token"))
                client_id = keyring.get_password(ACCOUNT_KEYRING_SERVICE, _account_key(meta["uuid"], "client_id")) or meta.get("client_id")
                if refresh and client_id:
                    account = microsoft_refresh_login(client_id, refresh)
                    saved = save_account_refresh_token(account) if account.get("refresh_token") else False
                    root.after(0, lambda: _set_active_microsoft_account(
                        account, saved=saved, log_message=f"Автоматически восстановлен Microsoft-сеанс: {account['username']}"))
                    return

            # Migrate credentials created by older launcher builds (single-account key names).
            if not accounts:
                legacy_refresh = keyring.get_password(ACCOUNT_KEYRING_SERVICE, "refresh_token")
                legacy_client = keyring.get_password(ACCOUNT_KEYRING_SERVICE, "client_id")
                if legacy_refresh and legacy_client:
                    account = microsoft_refresh_login(legacy_client, legacy_refresh)
                    saved = save_account_refresh_token(account) if account.get("refresh_token") else False
                    if saved:
                        for legacy_key in ("refresh_token", "client_id"):
                            try:
                                keyring.delete_password(ACCOUNT_KEYRING_SERVICE, legacy_key)
                            except Exception:
                                pass
                    root.after(0, lambda: _set_active_microsoft_account(
                        account, saved=saved, log_message=f"Восстановлен старый Microsoft-сеанс: {account['username']}"))
        except Exception as ex:
            msg = str(ex)
            root.after(0, lambda: debug_log(f"Автовход Microsoft не выполнен: {msg}"))
    threading.Thread(target=worker, daemon=True).start()

# ---------- настройки ----------
def open_settings():
    win = tk.Toplevel(root)
    S(win, role="bg")
    win.title("Настройки")
    win.grab_set()
    sf = tk.Frame(win)
    S(sf, role="bg")
    sf.pack(fill="both", expand=True, padx=10, pady=10)
    sf.columnconfigure(1, weight=1)

    def lbl(r, text):
        S(tk.Label(sf, text=text), role="bg", fg=pal["fg"]).grid(row=r, column=0, sticky="w")

    lbl(0, "Ник:")
    e_nick = S(tk.Entry(sf), role="accent", fg=pal["fg"], insertbackground=pal["fg"])
    e_nick.insert(0, cfg["nickname"])
    e_nick.grid(row=0, column=1, sticky="we", pady=3)

    lbl(1, "Microsoft Client ID (скрыт звёздочками):")
    # Client ID remains editable/pastable, but is masked on screen like a credential.
    e_ms_client = S(tk.Entry(sf, width=45, show="*"), role="accent", fg=pal["fg"], insertbackground=pal["fg"])
    e_ms_client.insert(0, cfg.get("ms_client_id", ""))
    e_ms_client.grid(row=1, column=1, sticky="we", pady=3)
    S(tk.Label(sf, text="Нужен Client ID приложения для личных Microsoft-аккаунтов. Для входа по коду открой App registrations → Authentication → Advanced settings и включи Allow public client flows = Yes. Для браузерного входа настрой Mobile and desktop applications → http://localhost. Для запоминания аккаунтов установи: pip install keyring.",
               wraplength=420), role="bg", fg=pal["notinstalled"]).grid(
                   row=2, column=0, columnspan=2, sticky="w")

    lbl(3, "Ссылка на папку Google Drive:")
    e_link = S(tk.Entry(sf, width=45), role="accent", fg=pal["fg"], insertbackground=pal["fg"])
    e_link.insert(0, cfg["gdrive_link"])
    e_link.grid(row=3, column=1, sticky="we", pady=3)

    lbl(4, "Память:")
    mf = tk.Frame(sf); S(mf, role="bg"); mf.grid(row=4, column=1, sticky="w", pady=3)
    mem_var = tk.StringVar()
    use_mb = tk.BooleanVar(value=False)
    def mem_to_entry():
        mb = cfg.get("memory_mb", 2048)
        mem_var.set(str(mb) if use_mb.get() else str(mb / 1024).rstrip("0").rstrip(".").replace(".", ","))
    def entry_to_mb():
        try:
            val = float(mem_var.get().replace(",", ".").strip())
        except ValueError:
            return cfg.get("memory_mb", 2048)
        return max(256, int(val) if use_mb.get() else int(val * 1024))
    S(tk.Spinbox(mf, from_=1, to=65536, textvariable=mem_var, width=8),
      role="accent", fg=pal["fg"]).pack(side="left")
    S(tk.Checkbutton(mf, text="в МБ (снять — в ГБ)", variable=use_mb, command=mem_to_entry),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).pack(side="left", padx=6)
    mem_to_entry()

    javas = list_javas()
    lbl(5, "Версия Java:")
    java_var = tk.StringVar(value=cfg["java"] if cfg["java"] in ["Авто"] + [n for n, _ in javas] else "Авто")
    java_row = tk.Frame(sf); S(java_row, role="bg")
    java_row.grid(row=5, column=1, sticky="w", pady=3)
    java_options = ["Авто"] + [n for n, _ in javas]
    om = S(tk.OptionMenu(java_row, java_var, *java_options),
           role="accent", fg=pal["fg"], activebackground=pal["accent"])
    om["menu"].configure(bg=pal["accent"], fg=pal["fg"])
    om.pack(side="left")
    install_java_major = tk.StringVar(value="21")
    S(tk.OptionMenu(java_row, install_java_major, "8", "16", "17", "21", "25"),
      role="accent", fg=pal["fg"]).pack(side="left", padx=(8, 2))
    def refresh_java_choices(installed_path=None):
        names = ["Авто"] + [n for n, _ in list_javas()]
        menu = om["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_radiobutton(label=name, variable=java_var, value=name)
        if installed_path:
            found = next((n for n, path in list_javas() if path == installed_path), None)
            if found:
                java_var.set(found)
        elif java_var.get() not in names:
            java_var.set("Авто")
    def install_java_from_settings():
        major = int(install_java_major.get())
        def worker():
            try:
                path = download_java(major, on_progress=lambda m: root.after(0, lambda msg=m: (debug_log(msg), ui_progress(msg))))
                root.after(0, lambda: (refresh_java_choices(path), log(f"Java {major} установлена через лаунчер"),
                                       status.config(text=f"Java {major} готова")))
            except Exception as ex:
                msg = str(ex)
                root.after(0, lambda: error("Не удалось установить Java: " + msg))
        threading.Thread(target=worker, daemon=True).start()
    S(tk.Button(java_row, text="Установить Java", command=install_java_from_settings),
      role="accent", fg=pal["fg"]).pack(side="left", padx=3)

    # Graphics backend selector (Minecraft 26.2+). OpenGL is the safe default
    # for older GPUs; users can explicitly test Vulkan or the game's Default.
    graphics_labels = {
        "По умолчанию": "default",
        "OpenGL": "opengl",
        "Vulkan": "vulkan",
    }
    current_backend = str(cfg.get("graphics_backend", "opengl")).lower()
    if current_backend not in graphics_labels.values():
        current_backend = "opengl"
    graphics_label_by_value = {value: label for label, value in graphics_labels.items()}
    graphics_var = tk.StringVar(value=graphics_label_by_value[current_backend])
    S(tk.Label(java_row, text="Графика (26.2+):"), role="bg", fg=pal["fg"]).pack(side="left", padx=(10, 2))
    graphics_menu = S(tk.OptionMenu(java_row, graphics_var, *graphics_labels.keys()),
                      role="accent", fg=pal["fg"], activebackground=pal["accent"])
    graphics_menu["menu"].configure(bg=pal["accent"], fg=pal["fg"])
    graphics_menu.pack(side="left", padx=2)

    use_van = tk.BooleanVar(value=cfg.get("show_vanilla", True))
    S(tk.Checkbutton(sf, text="Показывать ванильные версии Mojang в списке",
                     variable=use_van),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=6, column=0, columnspan=2, sticky="w", pady=3)

    use_fab = tk.BooleanVar(value=cfg.get("show_fabric", True))
    S(tk.Checkbutton(sf, text="Показывать Fabric в списке",
                     variable=use_fab),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=7, column=0, columnspan=2, sticky="w", pady=3)

    use_frge = tk.BooleanVar(value=cfg.get("show_forge", True))
    S(tk.Checkbutton(sf, text="Показывать Forge в списке",
                     variable=use_frge),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=8, column=0, columnspan=2, sticky="w", pady=3)

    use_ajava = tk.BooleanVar(value=cfg.get("auto_java", True))
    S(tk.Checkbutton(sf, text="Автоматически скачивать нужную Java (Temurin)",
                     variable=use_ajava),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=9, column=0, columnspan=2, sticky="w", pady=3)

    title_key = {"text_color": "fg", "bg_color": "bg", "accent_color": "accent"}

    def make_color_row(r, label, key, title):
        lbl(r, label)
        rowf = tk.Frame(sf); S(rowf, role="bg"); rowf.grid(row=r, column=1, sticky="w", pady=3)
        def pick():
            cur = cfg.get(key) or pal[title_key[key]]
            c = colorchooser.askcolor(color=cur, parent=win, title=title)
            if c and c[1]:
                cfg[key] = c[1]
                save_cfg(cfg)
                refresh_palette()
                restyle(); update_status()
        def do_reset():
            cfg[key] = ""
            save_cfg(cfg)
            refresh_palette()
            restyle(); update_status()
        S(tk.Button(rowf, text="Выбрать...", command=pick),
          role="accent", fg=pal["fg"]).pack(side="left", padx=2)
        S(tk.Button(rowf, text="Сброс", command=do_reset),
          role="accent", fg=pal["fg"]).pack(side="left")

    make_color_row(8, "Цвет текста:",  "text_color",   "Цвет текста")
    make_color_row(9, "Цвет фона:",    "bg_color",     "Цвет фона")
    make_color_row(10, "Цвет панелей:", "accent_color", "Цвет панелей")

    license_var = tk.BooleanVar(value=cfg.get("require_license", False))
    S(tk.Checkbutton(sf, text="Использовать только лицензионный аккаунт (запретить офлайн-запуск)",
                     variable=license_var), role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(
                         row=11, column=0, columnspan=2, sticky="w", pady=3)

    neo_var = tk.BooleanVar(value=cfg.get("show_neoforge", True))
    S(tk.Checkbutton(sf, text="Показывать NeoForge в списке версий", variable=neo_var),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=12, column=0, columnspan=2, sticky="w", pady=3)

    skin_frame = tk.Frame(sf); S(skin_frame, role="bg")
    skin_frame.grid(row=13, column=0, columnspan=2, sticky="we", pady=4)
    S(tk.Label(skin_frame, text="Скины аккаунта (нужен вход Microsoft):"), role="bg", fg=pal["fg"]).pack(anchor="w")
    skin_list = tk.Listbox(skin_frame, height=3, exportselection=False)
    S(skin_list, role="accent", fg=pal["fg"]).pack(fill="x", pady=2)
    skin_preview_photo = {"image": None, "window": None, "canvas": None,
                          "texture": None, "angle": 0.55, "tilt": -0.12,
                          "zoom": 7.0, "drag": None}

    def show_skin_preview(source, title="3D-предпросмотр скина"):
        """Строит интерактивную 3D-модель Minecraft из PNG-текстуры."""
        try:
            import base64
            if isinstance(source, bytes):
                photo = tk.PhotoImage(data=base64.b64encode(source))
            elif isinstance(source, str) and source.startswith(("https://", "http://")):
                photo = tk.PhotoImage(data=base64.b64encode(_http_get(source, timeout=25)))
            else:
                photo = tk.PhotoImage(file=source)
            if photo.width() not in (64, 128) or photo.height() not in (32, 64, 128):
                raise ValueError("Ожидается PNG-текстура Minecraft 64×64 или совместимого размера.")
            # 128x128 — HD skin: координаты UV масштабируются вдвое.
            scale = 2 if photo.width() >= 128 else 1
            tw, th = photo.width(), photo.height()
            if skin_preview_photo["window"] is not None:
                try:
                    if skin_preview_photo["window"].winfo_exists():
                        skin_preview_photo["window"].destroy()
                except Exception:
                    pass

            preview = tk.Toplevel(win)
            preview.title(title)
            preview.geometry("560x620")
            S(preview, role="bg")
            canvas = tk.Canvas(preview, width=520, height=520,
                               highlightthickness=0, bg=pal["bg"])
            canvas.pack(fill="both", expand=True, padx=12, pady=(12, 4))
            hint = tk.Label(preview, text="Зажми левую кнопку мыши и двигай для вращения • Колесо — масштаб",
                            fg=pal["fg"], bg=pal["bg"])
            hint.pack(pady=(2, 10))
            state = {"angle": 0.55, "tilt": -0.12, "zoom": 7.0,
                     "drag": None, "photo": photo, "canvas": canvas}
            skin_preview_photo.update({"window": preview, "canvas": canvas,
                                       "texture": photo})

            def pixel(u, v):
                u = max(0, min(tw - 1, int(u)))
                v = max(0, min(th - 1, int(v)))
                try:
                    c = photo.get(u, v)
                    if isinstance(c, str):
                        c = tuple(int(x) for x in c.split())
                    return tuple(max(0, min(255, int(x))) for x in c[:3])
                except Exception:
                    return (150, 150, 150)

            def uv_color(rect, fx, fy):
                x, y, w, h = rect
                return pixel((x + min(.999, max(0, fx)) * w) * scale,
                             (y + min(.999, max(0, fy)) * h) * scale)

            def shade(c, factor):
                return tuple(max(0, min(255, int(v * factor))) for v in c)

            # Minecraft classic UV front panels (base layer); outer overlays are
            # represented by the same textured geometry for a clean preview.
            parts = [
                # name, box(x0,x1,y0,y1,z0,z1), front UV(x,y,w,h), grid
                ("head", (-4,4, 24,32, -4,4), (8,8,8,8), (8,8)),
                ("body", (-4,4, 12,24, -2,2), (20,20,8,12), (8,12)),
                ("right_arm", (-8,-4, 12,24, -2,2), (44,20,4,12), (4,12)),
                ("left_arm", (4,8, 12,24, -2,2), (44,20,4,12), (4,12)),
                ("right_leg", (-4,0, 0,12, -2,2), (4,20,4,12), (4,12)),
                ("left_leg", (0,4, 0,12, -2,2), (4,20,4,12), (4,12)),
            ]

            def project(p):
                x, y, z = p
                ca, sa = math.cos(state["angle"]), math.sin(state["angle"])
                x, z = x*ca - z*sa, x*sa + z*ca
                ct, st = math.cos(state["tilt"]), math.sin(state["tilt"])
                y, z = y*ct - z*st, y*st + z*ct
                return (260 + x*state["zoom"], 285 - y*state["zoom"], z)

            def draw():
                canvas.delete("all")
                canvas.configure(bg=pal["bg"])
                faces = []
                for name, (x0,x1,y0,y1,z0,z1), uv, grid in parts:
                    # six faces; front (z=-) receives the actual UV texture grid.
                    corners = {
                        "front": [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0)],
                        "back": [(x1,y0,z1),(x0,y0,z1),(x0,y1,z1),(x1,y1,z1)],
                        "left": [(x0,y0,z1),(x0,y0,z0),(x0,y1,z0),(x0,y1,z1)],
                        "right": [(x1,y0,z0),(x1,y0,z1),(x1,y1,z1),(x1,y1,z0)],
                        "top": [(x0,y1,z0),(x1,y1,z0),(x1,y1,z1),(x0,y1,z1)],
                        "bottom": [(x0,y0,z1),(x1,y0,z1),(x1,y0,z0),(x0,y0,z0)]
                    }
                    for side, pts in corners.items():
                        pp = [project(p) for p in pts]
                        depth = sum(p[2] for p in pp) / 4
                        if side == "front":
                            gx, gy = grid
                            for iy in range(gy):
                                for ix in range(gx):
                                    fx0, fx1 = ix/gx, (ix+1)/gx
                                    fy0, fy1 = iy/gy, (iy+1)/gy
                                    p0 = (x0+(x1-x0)*fx0, y0+(y1-y0)*fy0, z0)
                                    p1 = (x0+(x1-x0)*fx1, y0+(y1-y0)*fy0, z0)
                                    p2 = (x0+(x1-x0)*fx1, y0+(y1-y0)*fy1, z0)
                                    p3 = (x0+(x1-x0)*fx0, y0+(y1-y0)*fy1, z0)
                                    col = uv_color(uv, (ix+.5)/gx, (iy+.5)/gy)
                                    faces.append((sum(project(p)[2] for p in (p0,p1,p2,p3))/4,
                                                  [project(p) for p in (p0,p1,p2,p3)], col, True))
                        else:
                            base = uv_color(uv, .5, .5)
                            factor = {"back":.62,"left":.72,"right":.82,"top":1.12,"bottom":.48}[side]
                            faces.append((depth, pp, shade(base, factor), False))
                # painter's algorithm: far surfaces first
                for _, pts, col, textured in sorted(faces, key=lambda f:f[0]):
                    flat = [v for p in pts for v in p[:2]]
                    color = "#%02x%02x%02x" % col
                    canvas.create_polygon(*flat, fill=color,
                                           outline=("#242424" if textured else "#333333"),
                                           width=1 if textured else 2)

            def press(event):
                state["drag"] = (event.x, event.y)
            def drag(event):
                if state["drag"] is None:
                    return
                ox, oy = state["drag"]
                state["angle"] += (event.x - ox) * .012
                state["tilt"] = max(-1.1, min(1.1, state["tilt"] + (event.y - oy) * .009))
                state["drag"] = (event.x, event.y)
                draw()
            def release(_event):
                state["drag"] = None
            def wheel(event):
                delta = 1.12 if getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4 else .89
                state["zoom"] = max(3.5, min(13, state["zoom"] * delta))
                draw()
            canvas.bind("<ButtonPress-1>", press)
            canvas.bind("<B1-Motion>", drag)
            canvas.bind("<ButtonRelease-1>", release)
            canvas.bind("<MouseWheel>", wheel)
            canvas.bind("<Button-4>", wheel)
            canvas.bind("<Button-5>", wheel)
            draw()
        except Exception as ex:
            error("Не удалось построить 3D-модель скина: " + str(ex))

    def reload_skins():
        skin_list.delete(0, "end")
        if not active_account:
            skin_list.insert("end", "Войди через Microsoft, чтобы увидеть скины")
            return
        def worker():
            try:
                skins = get_account_skins(active_account)
                def fill():
                    skin_list.delete(0, "end")
                    for skin in skins:
                        skin_list.insert("end", f"{skin.get('state','')}: {skin.get('variant','classic')} — {skin.get('url','')}")
                    if not skins:
                        skin_list.insert("end", "У профиля пока нет скинов")
                root.after(0, fill)
            except Exception as ex:
                msg = str(ex); root.after(0, lambda: (skin_list.delete(0, "end"), skin_list.insert("end", msg)))
        threading.Thread(target=worker, daemon=True).start()
    def preview_selected_skin():
        selected = skin_list.curselection()
        if not selected:
            error("Сначала выбери скин в списке")
            return
        if not active_account:
            error("Сначала войди через Microsoft")
            return
        def worker():
            try:
                skins = get_account_skins(active_account)
                # Listbox contains only active profile skins in the same order.
                idx = selected[0]
                if idx >= len(skins):
                    raise RuntimeError("Выбранный скин не найден в профиле")
                url = skins[idx].get("url")
                if not url:
                    raise RuntimeError("У этого скина отсутствует URL текстуры")
                image_data = _http_get(url, timeout=25)
                root.after(0, lambda data=image_data: show_skin_preview(data, "Предпросмотр скина аккаунта"))
            except Exception as ex:
                msg = str(ex); root.after(0, lambda: error("Не удалось открыть предпросмотр: " + msg))
        threading.Thread(target=worker, daemon=True).start()
    def choose_skin_file():
        if not active_account:
            error("Сначала войди через Microsoft в лицензионный аккаунт")
            return
        path = filedialog.askopenfilename(parent=win, title="Выбери PNG скин Minecraft",
                                          filetypes=[("PNG", "*.png")])
        if not path: return
        try:
            # Локальный файл можно посмотреть до отправки на сервер.
            show_skin_preview(path, "Предпросмотр выбранного скина")
        except Exception:
            pass
        variant = skin_variant.get()
        def worker():
            try:
                upload_account_skin(active_account, path, variant)
                root.after(0, lambda: (log("Скин успешно загружен в Minecraft-профиль"), reload_skins(),
                                       status.config(text="Скин обновлён")))
            except Exception as ex:
                msg = str(ex); root.after(0, lambda: error("Не удалось сменить скин: " + msg))
        threading.Thread(target=worker, daemon=True).start()
    skin_list.bind("<<ListboxSelect>>", lambda _event: preview_selected_skin())
    skin_variant = tk.StringVar(value="classic")
    S(tk.Radiobutton(skin_frame, text="Classic", variable=skin_variant, value="classic"), role="bg", fg=pal["fg"]).pack(side="left")
    S(tk.Radiobutton(skin_frame, text="Slim", variable=skin_variant, value="slim"), role="bg", fg=pal["fg"]).pack(side="left")
    S(tk.Button(skin_frame, text="Обновить список", command=reload_skins), role="accent", fg=pal["fg"]).pack(side="left", padx=3)
    S(tk.Button(skin_frame, text="Просмотр", command=preview_selected_skin), role="accent", fg=pal["fg"]).pack(side="left", padx=3)
    S(tk.Button(skin_frame, text="Загрузить скин PNG", command=choose_skin_file), role="accent", fg=pal["fg"]).pack(side="left", padx=3)
    reload_skins()

    use_ignore = tk.BooleanVar(value=cfg.get("ignore_missing_libs", False))
    S(tk.Checkbutton(sf, text="Игнорировать отсутствие библиотек (запускать даже без них)",
                     variable=use_ignore),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=14, column=0, columnspan=2, sticky="w", pady=3)

    use_err = tk.BooleanVar(value=cfg.get("errors_in_window", False))
    S(tk.Checkbutton(sf, text="Все ошибки и сообщения — в одном окне (журнал)", variable=use_err),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=15, column=0, columnspan=2, sticky="w", pady=3)

    use_dbg = tk.BooleanVar(value=cfg.get("debug_console", True))
    S(tk.Checkbutton(sf, text="Консоль отладки включена", variable=use_dbg),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).grid(row=16, column=0, columnspan=2, sticky="w", pady=3)

    tf = tk.Frame(sf); S(tf, role="bg"); tf.grid(row=17, column=0, columnspan=2, sticky="we", pady=6)
    use_dark = tk.BooleanVar(value=cfg["dark_theme"])
    def set_dark():
        cfg["dark_theme"] = use_dark.get()
        refresh_palette(reset=True)
        save_cfg(cfg)
        apply_theme(); update_status()
    S(tk.Checkbutton(tf, text="Тёмная тема", variable=use_dark, command=set_dark),
      role="bg", fg=pal["fg"], selectcolor=pal["accent"]).pack(anchor="w")

    def set_photo_theme():
        p = filedialog.askopenfilename(title="Выбери фото для темы",
                                       filetypes=[("Изображения", "*.png *.jpg *.jpeg *.bmp *.gif *.webp")])
        if not p:
            return
        shutil.copy(p, THEME_IMG)
        cfg["dark_theme"] = False
        use_dark.set(False)
        refresh_palette(reset=True)
        save_cfg(cfg); apply_theme(); update_status()
        log("Фото-тема применена (сохранена как theme.png рядом с лаунчером)")
    S(tk.Button(tf, text="Сделать тему из фото...", command=set_photo_theme),
      role="accent", fg=pal["fg"]).pack(anchor="w", pady=2)
    S(tk.Label(tf, text="Для фото-темы нужен Pillow: pip install pillow"),
      role="bg", fg=pal["notinstalled"]).pack(anchor="w")

    def apply_changes(*_):
        cfg["nickname"] = e_nick.get().strip() or "Player"
        cfg["gdrive_link"] = e_link.get().strip()
        cfg["ms_client_id"] = e_ms_client.get().strip()
        cfg["memory_mb"] = entry_to_mb()
        cfg["java"] = java_var.get()
        cfg["graphics_backend"] = graphics_labels.get(graphics_var.get(), "opengl")
        cfg["show_vanilla"] = use_van.get()
        cfg["show_fabric"] = use_fab.get()
        cfg["show_forge"] = use_frge.get()
        cfg["show_neoforge"] = neo_var.get()
        cfg["require_license"] = license_var.get()
        cfg["auto_java"] = use_ajava.get()
        cfg["ignore_missing_libs"] = use_ignore.get()
        cfg["errors_in_window"] = use_err.get()
        cfg["debug_console"] = use_dbg.get()
        if cfg["debug_console"]:
            dbg_btn.pack_configure(fill="x", pady=2)
        else:
            dbg_btn.pack_forget()
        save_cfg(cfg); update_status(); rebuild_list()
    e_nick.bind("<KeyRelease>", apply_changes)
    e_ms_client.bind("<KeyRelease>", apply_changes)
    e_link.bind("<KeyRelease>", apply_changes)
    mem_var.trace_add("write", apply_changes)
    java_var.trace_add("write", apply_changes)
    graphics_var.trace_add("write", apply_changes)
    use_van.trace_add("write", apply_changes)
    use_fab.trace_add("write", apply_changes)
    use_frge.trace_add("write", apply_changes)
    neo_var.trace_add("write", apply_changes)
    license_var.trace_add("write", apply_changes)
    use_ajava.trace_add("write", apply_changes)
    use_ignore.trace_add("write", apply_changes)
    use_err.trace_add("write", apply_changes)
    use_dbg.trace_add("write", apply_changes)

    btns = tk.Frame(sf); S(btns, role="bg"); btns.grid(row=18, column=0, columnspan=2, pady=8, sticky="we")
    S(tk.Button(btns, text="Проверить папку Drive", command=lambda: refresh_remote_async()),
      role="accent", fg=pal["fg"]).pack(side="left", padx=2)
    S(tk.Button(btns, text="Обновить каталог Mojang", command=lambda: refresh_vanilla_async()),
      role="accent", fg=pal["fg"]).pack(side="left", padx=2)
    S(tk.Button(btns, text="Закрыть", command=win.destroy),
      role="accent", fg=pal["fg"]).pack(side="left")

def refresh_palette(reset=False):
    if reset:
        pal.clear()
        pal.update(PALETTES["dark"] if cfg.get("dark_theme") else PALETTES["light"])
    if cfg.get("text_color"):
        pal["fg"] = pal["installed"] = cfg["text_color"]
    if cfg.get("bg_color"):
        pal["bg"] = cfg["bg_color"]
    if cfg.get("accent_color"):
        pal["accent"] = cfg["accent_color"]

def mk_btn(text, cmd):
    b = S(tk.Button(frm, text=text, command=cmd),
          role="accent", fg=pal["fg"])
    b.pack(fill="x", pady=2)
    return b

mk_btn("⚙ Настройки (ник, ссылка, память, Java, цвета, тема)", open_settings)
mk_btn("🔄 Обновить каталоги (Mojang + Drive)",
       lambda: (rebuild_list(), refresh_vanilla_async(silent=True), refresh_remote_async(silent=True)))
mk_btn("⬇ Установить выбранное (Mojang / Fabric / Forge / NeoForge / Drive)", install_selected_or_vanilla)
mk_btn("🔐 Войти через Microsoft в браузере", microsoft_login)
mk_btn("🔑 Войти через Microsoft по коду", microsoft_device_login_ui)
mk_btn("👥 Управление Microsoft-аккаунтами", open_microsoft_accounts)
mk_btn("↩ Выйти из текущего Microsoft-аккаунта", microsoft_logout)
mk_btn("📋 Журнал (ошибки и сообщения)", lambda: log("Открыт журнал"))
dbg_btn = mk_btn("🛠 Консоль отладки", lambda: debug_log("Открыта консоль отладки"))
if not cfg.get("debug_console", True):
    dbg_btn.pack_forget()

def _choose_java_for_version(real):
    """Окно ручного выбора Java, если метаданных версии недостаточно."""
    path = filedialog.askopenfilename(
        parent=root,
        title=f"Выбери Java для Minecraft {real}",
        filetypes=[("Java executable", "javaw.exe java.exe java"), ("Все файлы", "*.*")]
    )
    if not path:
        status.config(text="Запуск отменён: Java не выбрана")
        return
    if os.path.basename(path).lower() not in ("javaw.exe", "java.exe", "java"):
        messagebox.showerror("Неверный файл Java",
                             "Выбери javaw.exe или java.exe из папки bin установленной Java.",
                             parent=root)
        root.after(100, lambda: _choose_java_for_version(real))
        return
    cfg["java"] = os.path.abspath(path)
    save_cfg(cfg)
    debug_log(f"Для {real} вручную выбрана Java: {path}")
    status.config(text="Java выбрана, запускаю Minecraft...")
    _start_game(real)


def play():
    global preparing
    entry = sel_entry()
    if not entry:
        error("Выбери версию")
        return
    text, real, kind, fid = entry
    if kind == "vanilla":
        error(f"«{real}» ещё не установлена — двойной клик по ней или кнопка «Установить выбранное»")
        return
    if kind == "fabric":
        error(f"Fabric {real} ещё не установлена — двойной клик по ней или кнопка «Установить выбранное»")
        return
    if kind == "forge":
        error(f"Forge {real} ещё не установлена — двойной клик по ней или кнопка «Установить выбранное»")
        return
    if kind == "neoforge":
        error(f"NeoForge {real} ещё не установлена — двойной клик по ней или кнопка «Установить выбранное»")
        return
    if cfg.get("require_license", False) and not active_account:
        error("Включён режим лицензии: войди через Microsoft-аккаунт с Minecraft Java Edition.")
        return
    if preparing:
        error("Уже идёт подготовка ресурсов — подожди")
        return
    preparing = True
    status.config(text=f"Проверяю ресурсы для {real}...")
    def worker():
        global preparing
        try:
            def prog(msg):
                root.after(0, lambda m=msg: (debug_log(m),
                                             status.config(text=m)))
            ensure_resources(real, os.path.join(VERSIONS_DIR, real), on_progress=prog)
            get_java_path(cfg.get("java", "Авто"), real,
                          os.path.join(VERSIONS_DIR, real))
            root.after(0, lambda: _start_game(real))
        except Exception as ex:
            err_text = str(ex)
            if err_text.startswith("JAVA_MANUAL_REQUIRED:"):
                clean_text = err_text.split(":", 1)[1].strip()
                root.after(0, lambda msg=clean_text: (
                    debug_log("Требуется ручной выбор Java: " + msg),
                    status.config(text="Нужно выбрать Java"),
                    messagebox.showwarning("Выбор Java", msg, parent=root),
                    _choose_java_for_version(real)
                ))
            else:
                root.after(0, lambda: (error(err_text),
                                       debug_log("ОШИБКА ПОДГОТОВКИ РЕСУРСОВ:\n" + err_text),
                                       status.config(text="Не удалось подготовить ресурсы")))
        finally:
            preparing = False
    threading.Thread(target=worker, daemon=True).start()

def _start_game(real):
    vdir = os.path.join(VERSIONS_DIR, real)
    try:
        debug_log("=" * 60)
        debug_log(f"Запуск: {real} | ник: {cfg['nickname']} | память: {cfg.get('memory_mb')} МБ")
        main_jar = find_main_jar(vdir, real)
        debug_log(f"Главный jar: {main_jar}")
        vjson = read_version_json(vdir, real)
        debug_log("version.json: " + ("найден" if vjson else "НЕ найден"))
        cp_count = len(collect_jars(LIBS_DIR, os.path.join(vdir, "libraries")))
        debug_log(f"Библиотек (jar) в classpath: {cp_count}")
        def on_out(line):
            root.after(0, lambda l=line: debug_log(l))
        def on_exit(code):
            root.after(0, lambda: (debug_log(f"--- Minecraft завершился с кодом {code} ---"),
                                   status.config(text=f"Игра закрылась (код {code})")))
        proc = launch(real, cfg["nickname"], cfg.get("memory_mb", 2048),
                      on_output=on_out, on_exit=on_exit, account=active_account)
        debug_log(f"Процесс запущен, PID: {proc.pid}")
        status.config(text=f"Игра запущена (PID {proc.pid})")
        root.iconify()
    except Exception as ex:
        error(f"Ошибка запуска: {ex}")
        debug_log(f"ОШИБКА ЗАПУСКА: {ex}")

mk_btn("▶ ИГРАТЬ", play).pack_configure(fill="x", pady=6)

apply_theme()
rebuild_list()
update_status()
update_titlebar()

def on_resize(event):
    global _resize_job
    if event.widget is not root or not os.path.exists(THEME_IMG):
        return
    if _resize_job:
        root.after_cancel(_resize_job)
    _resize_job = root.after(150, lambda: apply_bg_photo())
root.bind("<Configure>", on_resize)

refresh_vanilla_async(silent=True)
if cfg.get("gdrive_link"):
    refresh_remote_async(silent=True)
# Restore the previously selected Microsoft account without opening a browser.
root.after(400, restore_microsoft_account_async)
root.mainloop()
