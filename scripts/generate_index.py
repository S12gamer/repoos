#!/usr/bin/env python3
"""Genera repo/index-v1.json leyendo los APK de GitHub Releases."""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "repo"
META = ROOT / "metadata"
BASE = os.environ["REPO_BASE_URL"].rstrip("/") + "/"
MAX_VERSIONS = int(os.environ.get("MAX_VERSIONS", "3"))
TOKEN = os.environ.get("GITHUB_TOKEN")

SESSION = requests.Session()
SESSION.headers["Accept"] = "application/vnd.github+json"
if TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {TOKEN}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def badging(apk: Path) -> dict:
    proc = subprocess.run(
        ["aapt2", "dump", "badging", str(apk)], capture_output=True, text=True
    )
    out = proc.stdout
    if proc.returncode != 0 or "package:" not in out:
        print(proc.stdout[:2000])
        print(proc.stderr[:2000])
        sys.exit(f"[ERROR] aapt2 no pudo leer {apk.name} (código {proc.returncode})")

    def need(pattern: str) -> str:
        m = re.search(pattern, out)
        if not m:
            print("----- salida de aapt2 (primeras líneas) -----")
            print("\n".join(out.splitlines()[:25]))
            sys.exit(f"[ERROR] No se encontró {pattern!r} en badging de {apk.name}")
        return m.group(1)

    def optional_int(pattern: str, default: int) -> int:
        m = re.search(pattern, out)
        return int(m.group(1)) if m else default

    # (?<![A-Za-z]) evita que "sdkVersion" case dentro de "targetSdkVersion"
    min_sdk = optional_int(r"(?<![A-Za-z])(?:minSdkVersion|sdkVersion):'(\d+)'", 1)
    target_sdk = optional_int(r"targetSdkVersion:'(\d+)'", min_sdk)
    if not re.search(r"(?<![A-Za-z])(?:minSdkVersion|sdkVersion):'", out):
        print(f"[WARN] {apk.name}: aapt2 no reporta minSdk, se usa 1")

    vname = re.search(r"versionName='([^']*)'", out)
    native = re.search(r"^native-code: (.*)$", out, re.M)
    return {
        "packageName": need(r"package: name='([^']+)'"),
        "versionCode": int(need(r"versionCode='(\d+)'")),
        "versionName": vname.group(1) if vname else str(need(r"versionCode='(\d+)'")),
        "minSdk": min_sdk,
        "targetSdk": target_sdk,
        "abis": re.findall(r"'([\w-]+)'", native.group(1)) if native else [],
        "perms": sorted(set(re.findall(r"uses-permission: name='([^']+)'", out))),
    }


def signer_sha256(apk: Path) -> str:
    out = subprocess.check_output(
        ["apksigner", "verify", "--print-certs", str(apk)], text=True
    )
    m = re.search(r"certificate SHA-256 digest: ([0-9a-fA-F]+)", out)
    if not m:
        sys.exit(f"[ERROR] No se pudo leer el certificado de {apk.name}")
    return m.group(1).lower()


def fetch_releases(source: str, include_pre: bool):
    r = SESSION.get(f"https://api.github.com/repos/{source}/releases?per_page=20", timeout=30)
    r.raise_for_status()
    rels = [x for x in r.json() if not x["draft"] and (include_pre or not x["prerelease"])]
    return rels[:MAX_VERSIONS]


def download(url: str, dest: Path):
    with SESSION.get(url, stream=True, timeout=120, headers={"Accept": "application/octet-stream"}) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def main():
    if REPO.exists():
        shutil.rmtree(REPO)
    (REPO / "icons").mkdir(parents=True)
    (REPO / ".nojekyll").touch()

    apps, packages = [], {}
    now = int(time.time() * 1000)

    for app_dir in sorted(p for p in META.iterdir() if p.is_dir()):
        pkg = app_dir.name
        yml = app_dir / "app.yml"
        cfg = yaml.safe_load(yml.read_text(encoding="utf-8")) if yml.exists() else None
        if not cfg or "source" not in cfg:
            print(f"[WARN] {pkg}: app.yml vacío o sin 'source', se omite")
            continue
        pattern = re.compile(cfg.get("asset_pattern", r"\.apk$"))
        releases = fetch_releases(cfg["source"], cfg.get("include_prereleases", False))
        if not releases:
            print(f"[WARN] {pkg}: sin releases publicados, se omite")
            continue

        versions, signers = [], set()
        with tempfile.TemporaryDirectory() as tmp:
            for rel in releases:
                asset = next((a for a in rel["assets"] if pattern.search(a["name"])), None)
                if not asset:
                    print(f"[WARN] {pkg} {rel['tag_name']}: ningún asset coincide con el patrón")
                    continue
                apk = Path(tmp) / asset["name"]
                print(f"[INFO] Descargando {asset['name']} ...")
                download(asset["browser_download_url"], apk)

                info = badging(apk)
                if info["packageName"] != pkg:
                    sys.exit(f"[ERROR] El APK es {info['packageName']} pero la carpeta es {pkg}")
                signer = signer_sha256(apk)
                signers.add(signer)
                versions.append({
                    "versionCode": info["versionCode"],
                    "versionName": info["versionName"],
                    "apkName": asset["browser_download_url"],   # URL absoluta
                    "size": apk.stat().st_size,
                    "sha256": sha256_file(apk),
                    "signer": signer,
                    "minSdkVersion": info["minSdk"],
                    "targetSdkVersion": info["targetSdk"],
                    "nativecode": info["abis"],
                    "usesPermission": info["perms"],
                    "added": iso_to_ms(rel["published_at"]),
                    "changelog": (rel.get("body") or "")[:2000] or None,
                })

        if not versions:
            continue
        if len(signers) > 1:
            sys.exit(f"[ERROR] {pkg}: las versiones están firmadas con certificados distintos")

        versions.sort(key=lambda v: -v["versionCode"])
        packages[pkg] = versions

        icon_rel = None
        if (app_dir / "icon.png").exists():
            shutil.copy2(app_dir / "icon.png", REPO / "icons" / f"{pkg}.png")
            icon_rel = f"icons/{pkg}.png"

        shots = []
        shots_dir = app_dir / "screenshots"
        if shots_dir.is_dir():
            out_dir = REPO / "screenshots" / pkg
            out_dir.mkdir(parents=True, exist_ok=True)
            for img in sorted(shots_dir.glob("*.png")):
                shutil.copy2(img, out_dir / img.name)
                shots.append(f"screenshots/{pkg}/{img.name}")

        apps.append({
            "packageName": pkg,
            "name": cfg["name"],
            "summary": cfg.get("summary", ""),
            "description": cfg.get("description", "").strip(),
            "icon": icon_rel,
            "categories": cfg.get("categories", []),
            "license": cfg.get("license"),
            "webSite": cfg.get("webSite"),
            "sourceCode": cfg.get("sourceCode"),
            "added": min(v["added"] for v in versions),
            "lastUpdated": max(v["added"] for v in versions),
            "suggestedVersionCode": versions[0]["versionCode"],
            "screenshots": shots,
        })

    index = {
        "repo": {
            "name": os.environ.get("REPO_NAME", "Mi Tienda"),
            "description": "Repositorio oficial",
            "address": BASE,
            "icon": None,
            "version": 1,
            "timestamp": now,
        },
        "apps": apps,
        "packages": packages,
    }
    # Separadores compactos y bytes estables: la firma se calcula sobre este archivo exacto
    (REPO / "index-v1.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"[OK] Índice generado: {len(apps)} apps")


if __name__ == "__main__":
    main()
