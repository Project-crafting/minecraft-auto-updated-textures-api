#!/usr/bin/env python3
"""
Minecraft Auto-Updated Texture API Generator
Extracts textures and model definitions from official Mojang client JARs,
generates a comprehensive item-to-texture mapping API, and prepares a static
API directory for deployment to GitHub Pages.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path


MOJANG_VERSION_MANIFEST = (
    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
)

USER_AGENT = "MinecraftTextureApiBuilder/2.0 (+https://github.com)"


# ---------------------------------------------------------------------------
# HTTP Helpers
# ---------------------------------------------------------------------------

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url: str, destination: Path, expected_sha1: str = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = destination.with_suffix(".tmp")

    print(f"Downloading client JAR:")
    print(f"  URL: {url}")
    print(f"  Dest: {destination}")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    hasher = hashlib.sha1() if expected_sha1 else None

    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total else 0
        downloaded = 0

        with open(temp_dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if hasher:
                    hasher.update(chunk)

                if total > 0:
                    pct = (downloaded / total) * 100
                    print(
                        f"\r  {downloaded / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB ({pct:.1f}%)",
                        end="",
                        flush=True,
                    )

    print()
    if expected_sha1 and hasher:
        calculated_sha1 = hasher.hexdigest()
        if calculated_sha1.lower() != expected_sha1.lower():
            temp_dest.unlink(missing_ok=True)
            raise ValueError(
                f"SHA1 mismatch: expected {expected_sha1}, got {calculated_sha1}"
            )

    temp_dest.replace(destination)
    print("Download verified and complete.")


# ---------------------------------------------------------------------------
# Mojang Version Resolver
# ---------------------------------------------------------------------------

def get_target_version_info(target_version: str = "latest", channel: str = "release"):
    print("Fetching Mojang version manifest...")
    manifest = fetch_json(MOJANG_VERSION_MANIFEST)

    latest_info = manifest.get("latest", {})
    if not target_version or target_version.lower() == "latest":
        resolved_id = latest_info.get("release")
    elif target_version.lower() in ("latest_snapshot", "snapshot"):
        resolved_id = latest_info.get("snapshot")
    else:
        resolved_id = target_version

    for v in manifest.get("versions", []):
        if v["id"] == resolved_id:
            print(f"Found Minecraft version: {v['id']} ({v['type']})")
            version_meta = fetch_json(v["url"])
            return v["id"], v["type"], version_meta

    raise RuntimeError(
        f"Minecraft version '{resolved_id}' not found in Mojang manifest."
    )


# ---------------------------------------------------------------------------
# Model & Texture Resolver
# ---------------------------------------------------------------------------

def normalize_resource(val: str):
    if not isinstance(val, str):
        return None
    val = val.strip().removeprefix("#")
    if ":" in val:
        ns, path = val.split(":", 1)
    else:
        ns = "minecraft"
        path = val
    return ns, path


def find_item_model_references(obj, output=None):
    if output is None:
        output = []

    if isinstance(obj, dict):
        if "model" in obj:
            m = obj["model"]
            if isinstance(m, str):
                output.append(m)
            elif isinstance(m, dict):
                find_item_model_references(m, output)
        if "base" in obj and isinstance(obj["base"], str):
            output.append(obj["base"])
        for k, v in obj.items():
            if k not in ("model", "base"):
                find_item_model_references(v, output)
    elif isinstance(obj, list):
        for x in obj:
            find_item_model_references(x, output)

    return output


class ModelResolver:
    def __init__(self, assets_root: Path):
        self.assets_root = assets_root  # e.g. <dir>/assets
        self.model_cache = {}

    def get_model_file(self, namespace: str, model_name: str) -> Path:
        return self.assets_root / namespace / "models" / f"{model_name}.json"

    def get_texture_file(self, namespace: str, texture_name: str) -> Path:
        return self.assets_root / namespace / "textures" / f"{texture_name}.png"

    def load_model(self, namespace: str, model_name: str):
        key = (namespace, model_name)
        if key in self.model_cache:
            return self.model_cache[key]

        path = self.get_model_file(namespace, model_name)
        if not path.exists():
            self.model_cache[key] = None
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.model_cache[key] = data
                return data
        except Exception:
            self.model_cache[key] = None
            return None

    def resolve_model(self, namespace: str, model_name: str, visited=None):
        if visited is None:
            visited = set()

        key = (namespace, model_name)
        if key in visited:
            return {"models": [], "raw_textures": {}}
        visited.add(key)

        model = self.load_model(namespace, model_name)
        if not model:
            return {"models": [], "raw_textures": {}}

        models_list = [f"{namespace}:{model_name}"]
        raw_textures = {}

        # 1. Resolve parent first (inheritance)
        parent = model.get("parent")
        if isinstance(parent, str):
            p_norm = normalize_resource(parent)
            if p_norm:
                p_ns, p_name = p_norm
                p_res = self.resolve_model(p_ns, p_name, visited)
                models_list.extend(p_res.get("models", []))
                raw_textures.update(p_res.get("raw_textures", {}))

        # 2. Local textures override parent textures
        textures_dict = model.get("textures")
        if isinstance(textures_dict, dict):
            for k, v in textures_dict.items():
                if isinstance(v, dict) and "sprite" in v:
                    raw_textures[k] = v["sprite"]
                elif isinstance(v, str):
                    raw_textures[k] = v

        return {
            "models": models_list,
            "raw_textures": raw_textures,
        }


def resolve_special_entity_textures(item_name: str, item_def: dict, assets_root: Path):
    """
    Handles block-entity items rendered via special entity models (e.g. chest, shulker, shield).
    """
    textures = {}
    mc_textures = assets_root / "minecraft" / "textures"

    def scan_special(obj):
        if isinstance(obj, dict):
            t = obj.get("type", "")
            if t == "minecraft:chest":
                tex = obj.get("texture", "normal")
                tex_name = tex.split(":")[-1]
                path = f"entity/chest/{tex_name}"
                if (mc_textures / f"{path}.png").exists():
                    textures["special_chest"] = f"minecraft:{path}"
            elif t == "minecraft:shulker_box":
                tex = obj.get("texture", "shulker")
                tex_name = tex.split(":")[-1]
                path = f"entity/shulker/{tex_name}"
                if (mc_textures / f"{path}.png").exists():
                    textures["special_shulker"] = f"minecraft:{path}"
            elif t == "minecraft:shield":
                textures["special_shield"] = "minecraft:entity/shield/shield_base"
            elif t == "minecraft:trident":
                textures["special_trident"] = "minecraft:entity/trident/trident"
            elif t == "minecraft:banner":
                textures["special_banner"] = "minecraft:entity/banner/base"
            elif t == "minecraft:conduit":
                textures["special_conduit"] = "minecraft:entity/conduit/base"
            elif t == "minecraft:decorated_pot":
                textures["special_pot"] = "minecraft:entity/decorated_pot/decorated_pot_side"

            for v in obj.values():
                scan_special(v)
        elif isinstance(obj, list):
            for x in obj:
                scan_special(x)

    scan_special(item_def)
    return textures


def infer_category(item_name: str, primary_path: str):
    if "block" in primary_path or primary_path.startswith("textures/block/"):
        return "block"
    for tool_suffix in (
        "_sword", "_pickaxe", "_axe", "_shovel", "_hoe", "bow", "crossbow", "mace", "trident", "fishing_rod"
    ):
        if item_name.endswith(tool_suffix) or item_name == tool_suffix:
            return "tools"
    for armor_suffix in (
        "_helmet", "_chestplate", "_leggings", "_boots", "shield", "wolf_armor", "elytra"
    ):
        if item_name.endswith(armor_suffix) or item_name == armor_suffix:
            return "armor"
    if item_name.endswith("_spawn_egg"):
        return "spawn_eggs"
    if any(
        f in item_name
        for f in (
            "apple", "bread", "beef", "porkchop", "mutton", "chicken", "potato", "carrot", "stew", "soup",
            "pie", "cookie", "cake", "berries", "melon_slice", "golden_apple", "potion"
        )
    ):
        return "food"
    return "item"


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    version: str = "latest",
    channel: str = "release",
    base_url: str = "",
    output_dir: Path = None,
    cache_dir: Path = None,
    force: bool = False,
    check_only: bool = False,
):
    start_time = time.time()
    workspace_root = Path.cwd()

    if output_dir is None:
        output_dir = workspace_root / "public"
    if cache_dir is None:
        cache_dir = workspace_root / ".cache"

    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    version_file = workspace_root / "version.json"
    current_version_data = {}
    if version_file.exists():
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                current_version_data = json.load(f)
        except Exception:
            pass

    current_installed_version = current_version_data.get("version")

    # 1. Check version
    target_version_id, target_version_type, version_meta = get_target_version_info(
        version, channel
    )

    print("=" * 70)
    print(f" Minecraft Texture API Builder")
    print(f" Target Version:  {target_version_id} ({target_version_type})")
    print(f" Current Version: {current_installed_version or 'None'}")
    print("=" * 70)

    is_new_version = target_version_id != current_installed_version

    if not is_new_version and not force:
        print(f"\n[INFO] Version {target_version_id} is already up to date. No update required.")
        # Report for GitHub Actions
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output:
            with open(gh_output, "a") as f:
                f.write("updated=false\n")
                f.write(f"version={target_version_id}\n")
        if check_only:
            return 0
        print("Use --force to rebuild anyway.")
        return 0

    if check_only:
        print(f"\n[INFO] New version available: {target_version_id} (current: {current_installed_version})")
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output:
            with open(gh_output, "a") as f:
                f.write("updated=true\n")
                f.write(f"version={target_version_id}\n")
        return 0

    # 2. Locate or download client JAR
    client_download = version_meta.get("downloads", {}).get("client", {})
    client_url = client_download.get("url")
    client_sha1 = client_download.get("sha1")

    if not client_url:
        raise RuntimeError("No client JAR download URL in Mojang metadata.")

    # Check local pre-existing JARs
    possible_local_jars = [
        cache_dir / f"minecraft-{target_version_id}-client.jar",
        workspace_root / f"minecraft_{target_version_id}" / "download" / f"minecraft-{target_version_id}-client.jar",
    ]
    target_jar = cache_dir / f"minecraft-{target_version_id}-client.jar"

    jar_found = False
    for p in possible_local_jars:
        if p.exists() and p.stat().st_size > 10 * 1024 * 1024:
            if p != target_jar:
                print(f"Found existing local client JAR: {p}")
                shutil.copy2(p, target_jar)
            jar_found = True
            break

    if not jar_found:
        download_file(client_url, target_jar, client_sha1)

    # 3. Extract assets
    assets_extracted_dir = cache_dir / f"extracted_{target_version_id}" / "assets"
    minecraft_assets = assets_extracted_dir / "minecraft"

    if not minecraft_assets.exists() or force:
        print("\nExtracting Minecraft assets from client JAR...")
        if assets_extracted_dir.parent.exists():
            shutil.rmtree(assets_extracted_dir.parent)
        assets_extracted_dir.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(target_jar, "r") as jar:
            members = [m for m in jar.namelist() if m.startswith("assets/minecraft/")]
            print(f"Extracting {len(members):,} assets...")
            for m in members:
                jar.extract(m, assets_extracted_dir.parent)
        print("Asset extraction complete.")
    else:
        print("\nUsing cached extracted assets.")

    # 4. Resolve items
    items_dir = minecraft_assets / "items"
    if not items_dir.exists():
        raise RuntimeError(f"Items definition directory not found: {items_dir}")

    item_files = sorted(items_dir.glob("*.json"))
    print(f"\nProcessing {len(item_files):,} item definitions...")

    resolver = ModelResolver(assets_extracted_dir)

    # Output paths
    public_textures_dir = output_dir / "textures"
    public_items_dir = output_dir / "items"
    public_api_dir = output_dir / "api"
    public_single_items_dir = public_api_dir / "item"

    public_textures_dir.mkdir(parents=True, exist_ok=True)
    public_items_dir.mkdir(parents=True, exist_ok=True)
    public_single_items_dir.mkdir(parents=True, exist_ok=True)

    # Ensure .nojekyll for GitHub Pages
    (output_dir / ".nojekyll").touch(exist_ok=True)

    # Clean and copy all textures to public/textures/
    src_textures = minecraft_assets / "textures"
    print(f"Copying all vanilla textures into {public_textures_dir}...")
    if src_textures.exists():
        shutil.copytree(src_textures, public_textures_dir, dirs_exist_ok=True)

    clean_base_url = base_url.rstrip("/") if base_url else ""

    def make_url(rel_path: str):
        rel = rel_path.lstrip("/")
        return f"{clean_base_url}/{rel}" if clean_base_url else f"/{rel}"

    items_master_dict = {}

    for idx, item_file in enumerate(item_files, 1):
        item_name = item_file.stem
        item_id = f"minecraft:{item_name}"

        try:
            with open(item_file, "r", encoding="utf-8") as f:
                item_def = json.load(f)
        except Exception:
            continue

        model_refs = find_item_model_references(item_def)
        model_refs = list(dict.fromkeys(model_refs))

        resolved_models = []
        raw_textures = {}

        for m_ref in model_refs:
            norm = normalize_resource(m_ref)
            if not norm:
                continue
            ns, m_path = norm
            m_res = resolver.resolve_model(ns, m_path)
            resolved_models.extend(m_res.get("models", []))
            raw_textures.update(m_res.get("raw_textures", {}))

        # Check special models (chests, shulker, shields, etc.)
        special_textures = resolve_special_entity_textures(
            item_name, item_def, assets_extracted_dir
        )
        raw_textures.update(special_textures)

        # Resolve variable indirection (up to 10 hops)
        resolved_textures = {}
        for k, v in raw_textures.items():
            curr = v
            for _ in range(10):
                if isinstance(curr, str) and curr.startswith("#"):
                    curr = raw_textures.get(curr[1:], curr)
                else:
                    break
            if isinstance(curr, str) and not curr.startswith("#"):
                norm = normalize_resource(curr)
                if norm:
                    t_ns, t_path = norm
                    local_tex = assets_extracted_dir / t_ns / "textures" / f"{t_path}.png"
                    if local_tex.exists():
                        resolved_textures[k] = f"{t_ns}:{t_path}"

        # Determine primary texture
        primary_resource = None
        for role_key in (
            "layer0", "special_chest", "special_shulker", "special_shield", "special_trident",
            "texture", "all", "front", "side", "top", "particle", "cross", "end"
        ):
            if role_key in resolved_textures:
                primary_resource = resolved_textures[role_key]
                break

        if not primary_resource and resolved_textures:
            primary_resource = next(iter(resolved_textures.values()))

        # Build texture entry list
        texture_entries = []
        for role, res_loc in resolved_textures.items():
            t_ns, t_path = normalize_resource(res_loc)
            rel_tex_path = f"textures/{t_path}.png"
            texture_entries.append({
                "resource": res_loc,
                "role": role,
                "path": rel_tex_path,
                "raw_url": make_url(rel_tex_path),
            })

        # Primary texture paths & shortcut copy
        primary_rel_path = ""
        primary_raw_url = ""
        primary_item_path = f"items/{item_name}.png"
        primary_item_url = make_url(primary_item_path)

        if primary_resource:
            p_ns, p_path = normalize_resource(primary_resource)
            primary_rel_path = f"textures/{p_path}.png"
            primary_raw_url = make_url(primary_rel_path)

            source_file = assets_extracted_dir / p_ns / "textures" / f"{p_path}.png"
            dest_shortcut = public_items_dir / f"{item_name}.png"
            if source_file.exists():
                shutil.copyfile(source_file, dest_shortcut)

        category = infer_category(item_name, primary_rel_path)

        item_entry = {
            "id": item_id,
            "name": item_name,
            "category": category,
            "definition_file": f"assets/minecraft/items/{item_name}.json",
            "model_references": list(dict.fromkeys(resolved_models)),
            "raw_url": primary_raw_url,
            "item_url": primary_item_url,
            "primary_texture": primary_rel_path,
            "textures": texture_entries,
        }

        items_master_dict[item_id] = item_entry

        # Save single item API JSON
        single_item_file = public_single_items_dir / f"{item_name}.json"
        with open(single_item_file, "w", encoding="utf-8") as f:
            json.dump(item_entry, f, indent=2, ensure_ascii=False)

    # 5. Save master items JSON
    master_json_file = public_api_dir / "items.json"
    with open(master_json_file, "w", encoding="utf-8") as f:
        json.dump(items_master_dict, f, indent=2, ensure_ascii=False)
    print(f"Master API JSON written: {master_json_file} ({len(items_master_dict):,} items)")

    # 6. Save manifest JSON
    manifest_data = {
        "version": target_version_id,
        "version_type": target_version_type,
        "total_items": len(items_master_dict),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": clean_base_url,
        "endpoints": {
            "all_items": make_url("api/items.json"),
            "single_item_template": make_url("api/item/{id}.json"),
            "direct_item_image_template": make_url("items/{id}.png"),
            "textures_template": make_url("textures/{path}.png"),
        },
    }

    with open(public_api_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    # 7. Update version.json in workspace root
    version_payload = {
        "version": target_version_id,
        "version_type": target_version_type,
        "total_items": len(items_master_dict),
        "updated_at": manifest_data["updated_at"],
    }
    with open(version_file, "w", encoding="utf-8") as f:
        json.dump(version_payload, f, indent=2, ensure_ascii=False)

    # 8. Generate interactive index.html
    generate_web_portal(output_dir, manifest_data, items_master_dict)

    # 9. Generate Privacy Policy pages (/privicy and /privacy)
    generate_privacy_policy(output_dir, manifest_data)

    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print(" BUILD COMPLETE!")
    print(f" Version:       {target_version_id} ({target_version_type})")
    print(f" Items indexed: {len(items_master_dict):,}")
    print(f" Output folder: {output_dir}")
    print(f" Time elapsed:  {elapsed:.2f}s")
    print("=" * 70 + "\n")

    # Output for GitHub Actions
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write("updated=true\n")
            f.write(f"version={target_version_id}\n")
            f.write(f"total_items={len(items_master_dict)}\n")

    return 0


# ---------------------------------------------------------------------------
# Interactive Web Portal Generator
# ---------------------------------------------------------------------------

def generate_web_portal(output_dir: Path, manifest: dict, items: dict):
    index_file = output_dir / "index.html"
    version = manifest["version"]
    total = manifest["total_items"]
    base_url = manifest["base_url"] or ""

    # Create sample preview list (first 80 items for instant load before API fetch)
    sample_items = []
    for k, v in list(items.items())[:120]:
        sample_items.append({
            "id": v["id"],
            "name": v["name"],
            "category": v["category"],
            "raw_url": v["raw_url"],
            "item_url": v["item_url"],
            "primary_texture": v["primary_texture"],
        })

    sample_json = json.dumps(sample_items, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Minecraft Texture API | v{version}</title>
  <meta name="description" content="High-performance, auto-updating Minecraft textures API. Query raw image URLs and model definitions for any vanilla Minecraft item directly.">
  <link rel="icon" href="items/diamond.png" type="image/png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600&family=Inter:wght@400;500;600;700;800&family=Press+Start+2P&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-dark: #0a0d12;
      --bg-card: #131720;
      --bg-card-hover: #1c222e;
      --border: #232b38;
      --border-accent: #3b82f6;
      --diamond: #2ce2e6;
      --emerald: #2ecc71;
      --gold: #f59e0b;
      --redstone: #ef4444;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --code-bg: #0d1117;
      --font-pixel: 'Press Start 2P', monospace;
      --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'Fira Code', monospace;
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      background-color: var(--bg-dark);
      color: var(--text-main);
      font-family: var(--font-sans);
      line-height: 1.6;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}

    /* Header */
    header {{
      background: linear-gradient(180deg, #11151f 0%, #0a0d12 100%);
      border-bottom: 1px solid var(--border);
      padding: 2.5rem 1.5rem 2rem;
      text-align: center;
      position: relative;
    }}

    .header-badge-row {{
      display: flex;
      justify-content: center;
      gap: 0.75rem;
      margin-bottom: 1rem;
      flex-wrap: wrap;
    }}

    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.3rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      background: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-muted);
    }}

    .badge-diamond {{
      color: var(--diamond);
      border-color: rgba(44, 226, 230, 0.3);
      background: rgba(44, 226, 230, 0.08);
    }}

    .badge-emerald {{
      color: var(--emerald);
      border-color: rgba(46, 204, 113, 0.3);
      background: rgba(46, 204, 113, 0.08);
    }}

    h1 {{
      font-family: var(--font-pixel);
      font-size: 1.75rem;
      color: #ffffff;
      letter-spacing: -0.5px;
      margin-bottom: 0.75rem;
      text-shadow: 0 0 20px rgba(44, 226, 230, 0.3);
    }}

    .subtitle {{
      color: var(--text-muted);
      font-size: 1.05rem;
      max-width: 680px;
      margin: 0 auto 1.5rem;
    }}

    /* Navigation & Tabs */
    .nav-tabs {{
      display: flex;
      justify-content: center;
      gap: 0.5rem;
      margin-top: 1rem;
    }}

    .tab-btn {{
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      padding: 0.5rem 1.25rem;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.9rem;
      transition: all 0.2s;
    }}

    .tab-btn:hover {{
      color: var(--text-main);
      background: var(--bg-card);
    }}

    .tab-btn.active {{
      background: var(--bg-card);
      border-color: var(--diamond);
      color: var(--diamond);
    }}

    /* Main Container */
    main {{
      max-width: 1200px;
      width: 100%;
      margin: 0 auto;
      padding: 2rem 1.5rem;
      flex: 1;
    }}

    /* Quick Endpoints Cards */
    .quick-cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
      margin-bottom: 2.5rem;
    }}

    .quick-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem;
      transition: border-color 0.2s, transform 0.2s;
    }}

    .quick-card:hover {{
      border-color: rgba(44, 226, 230, 0.4);
      transform: translateY(-2px);
    }}

    .quick-card-title {{
      font-size: 0.85rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--diamond);
      margin-bottom: 0.5rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}

    .quick-card code {{
      display: block;
      background: var(--code-bg);
      padding: 0.5rem 0.75rem;
      border-radius: 6px;
      font-family: var(--font-mono);
      font-size: 0.8rem;
      color: #38bdf8;
      word-break: break-all;
      cursor: pointer;
      border: 1px solid #1e293b;
    }}

    .quick-card code:hover {{
      border-color: var(--diamond);
    }}

    /* Search & Filter Toolbar */
    .toolbar {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem;
      margin-bottom: 1.5rem;
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }}

    @media (min-width: 768px) {{
      .toolbar {{
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
      }}
    }}

    .search-box {{
      position: relative;
      flex: 1;
      max-width: 500px;
    }}

    .search-input {{
      width: 100%;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.65rem 1rem 0.65rem 2.5rem;
      color: var(--text-main);
      font-size: 0.95rem;
      outline: none;
      transition: border-color 0.2s;
    }}

    .search-input:focus {{
      border-color: var(--diamond);
      box-shadow: 0 0 0 2px rgba(44, 226, 230, 0.2);
    }}

    .search-icon {{
      position: absolute;
      left: 0.85rem;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 1rem;
    }}

    .filter-pills {{
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
    }}

    .filter-pill {{
      background: var(--code-bg);
      border: 1px solid var(--border);
      color: var(--text-muted);
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s;
    }}

    .filter-pill:hover, .filter-pill.active {{
      background: var(--diamond);
      color: #000;
      border-color: var(--diamond);
      font-weight: 600;
    }}

    /* Item Grid */
    .item-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      gap: 0.85rem;
      margin-bottom: 2rem;
    }}

    .item-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.85rem 0.5rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s;
      position: relative;
    }}

    .item-card:hover {{
      background: var(--bg-card-hover);
      border-color: var(--diamond);
      transform: translateY(-3px);
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.4);
    }}

    .item-icon-wrapper {{
      width: 56px;
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: center;
      margin-bottom: 0.6rem;
      background: rgba(0, 0, 0, 0.2);
      border-radius: 6px;
      padding: 4px;
    }}

    .item-icon {{
      max-width: 48px;
      max-height: 48px;
      image-rendering: pixelated;
      image-rendering: -moz-crisp-edges;
      image-rendering: crisp-edges;
      filter: drop-shadow(0 2px 4px rgba(0,0,0,0.5));
      transition: transform 0.15s;
    }}

    .item-card:hover .item-icon {{
      transform: scale(1.15);
    }}

    .item-name {{
      font-size: 0.75rem;
      font-weight: 600;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      width: 100%;
      margin-bottom: 0.2rem;
    }}

    .item-sub {{
      font-size: 0.65rem;
      color: var(--text-muted);
      font-family: var(--font-mono);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      width: 100%;
    }}

    /* Pagination */
    .pagination {{
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 0.5rem;
      margin: 2rem 0;
    }}

    .page-btn {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-main);
      padding: 0.5rem 1rem;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.85rem;
      transition: all 0.15s;
    }}

    .page-btn:hover:not(:disabled) {{
      background: var(--diamond);
      color: #000;
      border-color: var(--diamond);
    }}

    .page-btn:disabled {{
      opacity: 0.4;
      cursor: not-allowed;
    }}

    /* Modal / Inspector */
    .modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}

    .modal-overlay.open {{
      display: flex;
    }}

    .modal {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 14px;
      max-width: 650px;
      width: 100%;
      max-height: 90vh;
      overflow-y: auto;
      padding: 2rem;
      position: relative;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
    }}

    .modal-close {{
      position: absolute;
      top: 1.25rem;
      right: 1.25rem;
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 1.5rem;
      cursor: pointer;
      line-height: 1;
    }}

    .modal-close:hover {{
      color: #fff;
    }}

    .modal-header {{
      display: flex;
      align-items: center;
      gap: 1.5rem;
      margin-bottom: 1.5rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--border);
    }}

    .modal-sprite-preview {{
      width: 96px;
      height: 96px;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}

    .modal-sprite-preview img {{
      max-width: 72px;
      max-height: 72px;
      image-rendering: pixelated;
      image-rendering: -moz-crisp-edges;
      image-rendering: crisp-edges;
    }}

    .modal-title-group h2 {{
      font-size: 1.35rem;
      font-weight: 700;
      margin-bottom: 0.35rem;
    }}

    .modal-id {{
      font-family: var(--font-mono);
      font-size: 0.85rem;
      color: var(--diamond);
      margin-bottom: 0.5rem;
    }}

    .modal-section {{
      margin-bottom: 1.5rem;
    }}

    .modal-section-title {{
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      margin-bottom: 0.5rem;
    }}

    .copy-field {{
      display: flex;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
      margin-bottom: 0.5rem;
    }}

    .copy-field input {{
      flex: 1;
      background: transparent;
      border: none;
      padding: 0.5rem 0.75rem;
      color: #38bdf8;
      font-family: var(--font-mono);
      font-size: 0.8rem;
      outline: none;
    }}

    .copy-btn {{
      background: var(--border);
      border: none;
      color: var(--text-main);
      padding: 0 0.85rem;
      font-size: 0.75rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }}

    .copy-btn:hover {{
      background: var(--diamond);
      color: #000;
    }}

    .textures-list {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 0.75rem;
    }}

    .texture-thumb-box {{
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.5rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.3rem;
      text-align: center;
    }}

    .texture-thumb-box img {{
      width: 36px;
      height: 36px;
      image-rendering: pixelated;
    }}

    .texture-thumb-role {{
      font-size: 0.65rem;
      font-weight: 600;
      color: var(--gold);
    }}

    .texture-thumb-res {{
      font-size: 0.6rem;
      font-family: var(--font-mono);
      color: var(--text-muted);
      word-break: break-all;
    }}

    /* Documentation Section */
    .docs-section {{
      display: none;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 2rem;
      margin-bottom: 2rem;
    }}

    .docs-section.active {{
      display: block;
    }}

    .docs-section h2 {{
      font-size: 1.35rem;
      font-weight: 700;
      color: var(--diamond);
      margin-bottom: 1rem;
    }}

    .docs-section p {{
      color: var(--text-muted);
      margin-bottom: 1rem;
    }}

    .docs-code-block {{
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem;
      font-family: var(--font-mono);
      font-size: 0.85rem;
      color: #e2e8f0;
      overflow-x: auto;
      margin-bottom: 1.5rem;
    }}

    /* Toast Notification */
    .toast {{
      position: fixed;
      bottom: 2rem;
      right: 2rem;
      background: var(--diamond);
      color: #000;
      padding: 0.65rem 1.25rem;
      border-radius: 8px;
      font-weight: 700;
      font-size: 0.85rem;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      opacity: 0;
      transform: translateY(20px);
      transition: all 0.25s ease;
      z-index: 2000;
      pointer-events: none;
    }}

    .toast.show {{
      opacity: 1;
      transform: translateY(0);
    }}

    /* Footer */
    footer {{
      border-top: 1px solid var(--border);
      padding: 2rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
      font-size: 0.85rem;
    }}

    footer a {{
      color: var(--diamond);
      text-decoration: none;
    }}
  </style>
</head>
<body>

  <header>
    <div class="header-badge-row">
      <div class="badge badge-diamond">Minecraft v{version}</div>
      <div class="badge badge-emerald">{total:,} Items Indexed</div>
      <div class="badge">Auto-Updated via GitHub Actions</div>
    </div>
    <h1>MINECRAFT TEXTURE API</h1>
    <p class="subtitle">
      Query vanilla Minecraft textures, model definitions, and sprite image URLs directly via item IDs.
    </p>
    <div class="nav-tabs">
      <button class="tab-btn active" id="tabExplorerBtn" onclick="switchTab('explorer')">Item Explorer</button>
      <button class="tab-btn" id="tabDocsBtn" onclick="switchTab('docs')">API Reference</button>
      <a href="privicy/" class="tab-btn" style="text-decoration:none;">Privacy Policy</a>
    </div>
  </header>

  <main>
    <!-- Quick Endpoints -->
    <div class="quick-cards">
      <div class="quick-card">
        <div class="quick-card-title">Direct Item Image <span class="badge">RAW PNG</span></div>
        <code onclick="copyText(this.innerText)">{base_url}/items/diamond.png</code>
      </div>
      <div class="quick-card">
        <div class="quick-card-title">Single Item JSON <span class="badge">API</span></div>
        <code onclick="copyText(this.innerText)">{base_url}/api/item/diamond.json</code>
      </div>
      <div class="quick-card">
        <div class="quick-card-title">All Items Master API <span class="badge">{total:,} ITEMS</span></div>
        <code onclick="copyText(this.innerText)">{base_url}/api/items.json</code>
      </div>
    </div>

    <!-- Explorer View -->
    <div id="explorerTab">
      <div class="toolbar">
        <div class="search-box">
          <span class="search-icon">🔍</span>
          <input type="text" id="searchInput" class="search-input" placeholder="Search item by name or id (e.g. diamond, sword, furnace)...">
        </div>
        <div class="filter-pills">
          <button class="filter-pill active" onclick="setFilter('all')">All</button>
          <button class="filter-pill" onclick="setFilter('item')">Items</button>
          <button class="filter-pill" onclick="setFilter('block')">Blocks</button>
          <button class="filter-pill" onclick="setFilter('tools')">Tools & Weapons</button>
          <button class="filter-pill" onclick="setFilter('armor')">Armor</button>
          <button class="filter-pill" onclick="setFilter('food')">Food</button>
          <button class="filter-pill" onclick="setFilter('spawn_eggs')">Spawn Eggs</button>
        </div>
      </div>

      <div class="item-grid" id="itemGrid"></div>

      <div class="pagination" id="paginationControls">
        <button class="page-btn" id="prevPageBtn" onclick="changePage(-1)">← Previous</button>
        <span id="pageInfo" style="font-size:0.85rem; font-weight:600; color:var(--text-muted);">Page 1</span>
        <button class="page-btn" id="nextPageBtn" onclick="changePage(1)">Next →</button>
      </div>
    </div>

    <!-- Documentation View -->
    <div id="docsTab" class="docs-section">
      <h2>API Documentation</h2>
      <p>This API provides raw image assets and structured JSON metadata for all vanilla Minecraft items. It is automatically updated whenever Mojang releases a new Minecraft update.</p>

      <h3 style="font-size:1.1rem; color:#fff; margin:1.5rem 0 0.5rem;">1. Direct Item Image URL</h3>
      <p>Get any item's primary 2D sprite image raw URL instantly:</p>
      <div class="docs-code-block">{base_url}/items/{{item_name}}.png

# Examples:
# {base_url}/items/diamond.png
# {base_url}/items/netherite_sword.png
# {base_url}/items/furnace.png</div>

      <h3 style="font-size:1.1rem; color:#fff; margin:1.5rem 0 0.5rem;">2. Single Item Metadata Endpoint</h3>
      <p>Fetch the complete model and texture resolution for an individual item without downloading the entire item index:</p>
      <div class="docs-code-block">GET {base_url}/api/item/{{item_name}}.json

# Response Example:
{{
  "id": "minecraft:diamond",
  "name": "diamond",
  "category": "item",
  "definition_file": "assets/minecraft/items/diamond.json",
  "model_references": [
    "minecraft:item/diamond"
  ],
  "raw_url": "{base_url}/textures/item/diamond.png",
  "item_url": "{base_url}/items/diamond.png",
  "primary_texture": "textures/item/diamond.png",
  "textures": [
    {{
      "resource": "minecraft:item/diamond",
      "role": "layer0",
      "path": "textures/item/diamond.png",
      "raw_url": "{base_url}/textures/item/diamond.png"
    }}
  ]
}}</div>

      <h3 style="font-size:1.1rem; color:#fff; margin:1.5rem 0 0.5rem;">3. All Items Master Dictionary</h3>
      <p>Download the full JSON map of all {total:,} Minecraft items:</p>
      <div class="docs-code-block">GET {base_url}/api/items.json</div>

      <h3 style="font-size:1.1rem; color:#fff; margin:1.5rem 0 0.5rem;">4. Usage Examples</h3>
      <p><strong>JavaScript (fetch):</strong></p>
      <div class="docs-code-block">const res = await fetch('{base_url}/api/item/diamond.json');
const item = await res.json();
console.log('Diamond texture URL:', item.raw_url);</div>

      <p><strong>Python:</strong></p>
      <div class="docs-code-block">import requests

res = requests.get('{base_url}/api/item/diamond.json').json()
print("Raw URL:", res["raw_url"])</div>

      <p><strong>HTML Image Embed:</strong></p>
      <div class="docs-code-block">&lt;img src="{base_url}/items/diamond.png" alt="Diamond" width="32" height="32" style="image-rendering: pixelated;" /&gt;</div>
    </div>
  </main>

  <!-- Item Detail Modal -->
  <div class="modal-overlay" id="itemModal" onclick="if(event.target === this) closeModal()">
    <div class="modal">
      <button class="modal-close" onclick="closeModal()">×</button>
      <div class="modal-header">
        <div class="modal-sprite-preview">
          <img id="modalImg" src="" alt="">
        </div>
        <div class="modal-title-group">
          <h2 id="modalTitle">Diamond</h2>
          <div class="modal-id" id="modalId">minecraft:diamond</div>
          <span class="badge badge-diamond" id="modalCategory">item</span>
        </div>
      </div>

      <div class="modal-section">
        <div class="modal-section-title">Direct Image URL</div>
        <div class="copy-field">
          <input type="text" id="modalItemUrl" readonly>
          <button class="copy-btn" onclick="copyInput('modalItemUrl')">COPY</button>
        </div>
      </div>

      <div class="modal-section">
        <div class="modal-section-title">Single Item JSON API</div>
        <div class="copy-field">
          <input type="text" id="modalJsonUrl" readonly>
          <button class="copy-btn" onclick="copyInput('modalJsonUrl')">COPY</button>
        </div>
      </div>

      <div class="modal-section">
        <div class="modal-section-title">HTML & Markdown Embed</div>
        <div class="copy-field">
          <input type="text" id="modalEmbed" readonly>
          <button class="copy-btn" onclick="copyInput('modalEmbed')">COPY</button>
        </div>
      </div>

      <div class="modal-section">
        <div class="modal-section-title">Associated Textures & Layers</div>
        <div class="textures-list" id="modalTexturesList"></div>
      </div>

      <div class="modal-section">
        <div class="modal-section-title">Model References</div>
        <div class="docs-code-block" id="modalModels" style="margin-bottom:0; font-size:0.75rem;"></div>
      </div>
    </div>
  </div>

  <div class="toast" id="toast">Copied to clipboard!</div>

  <footer>
    <p>
      Minecraft assets and textures are the property of <strong>Mojang Studios / Microsoft</strong>.
      This project is an unofficial developer API for educational and toolmaking purposes.
    </p>
    <p style="margin-top:0.5rem;">
      Auto-generated for Minecraft <strong>v{version}</strong>. • <a href="privicy/">Privacy Policy</a>
    </p>
  </footer>

  <script>
    const INITIAL_ITEMS = {sample_json};
    const BASE_URL = "{base_url}";
    let allItems = INITIAL_ITEMS;
    let fullCatalog = null;
    let currentFilter = 'all';
    let searchQuery = '';
    let currentPage = 1;
    const ITEMS_PER_PAGE = 72;

    // Load full catalog asynchronously in background
    fetch('api/items.json')
      .then(res => res.json())
      .then(data => {{
        fullCatalog = data;
        allItems = Object.values(data);
        render();
      }})
      .catch(err => console.log('Loaded initial items sample:', err));

    function switchTab(tab) {{
      document.getElementById('explorerTab').style.display = tab === 'explorer' ? 'block' : 'none';
      document.getElementById('docsTab').classList.toggle('active', tab === 'docs');
      document.getElementById('tabExplorerBtn').classList.toggle('active', tab === 'explorer');
      document.getElementById('tabDocsBtn').classList.toggle('active', tab === 'docs');
    }}

    function setFilter(cat) {{
      currentFilter = cat;
      currentPage = 1;
      document.querySelectorAll('.filter-pill').forEach(btn => {{
        btn.classList.toggle('active', btn.innerText.toLowerCase() === cat.replace('_', ' ') || (cat === 'all' && btn.innerText === 'All'));
      }});
      render();
    }}

    document.getElementById('searchInput').addEventListener('input', (e) => {{
      searchQuery = e.target.value.toLowerCase().trim();
      currentPage = 1;
      render();
    }});

    function getFilteredItems() {{
      return allItems.filter(item => {{
        const matchesCategory = currentFilter === 'all' || item.category === currentFilter;
        const matchesSearch = !searchQuery || 
          item.name.toLowerCase().includes(searchQuery) || 
          item.id.toLowerCase().includes(searchQuery);
        return matchesCategory && matchesSearch;
      }});
    }}

    function render() {{
      const grid = document.getElementById('itemGrid');
      const filtered = getFilteredItems();
      const totalPages = Math.max(1, Math.ceil(filtered.length / ITEMS_PER_PAGE));
      if (currentPage > totalPages) currentPage = totalPages;

      const start = (currentPage - 1) * ITEMS_PER_PAGE;
      const paged = filtered.slice(start, start + ITEMS_PER_PAGE);

      grid.innerHTML = paged.map(item => `
        <div class="item-card" onclick="openItemModal('${{item.id}}')">
          <div class="item-icon-wrapper">
            <img class="item-icon" src="${{item.item_url || ('items/' + item.name + '.png')}}" alt="${{item.name}}" loading="lazy" onerror="this.src='items/barrier.png';">
          </div>
          <div class="item-name" title="${{item.name}}">${{item.name.replace(/_/g, ' ')}}</div>
          <div class="item-sub" title="${{item.id}}">${{item.name}}</div>
        </div>
      `).join('');

      document.getElementById('pageInfo').innerText = `Page ${{currentPage}} of ${{totalPages}} (${{filtered.length.toLocaleString()}} items)`;
      document.getElementById('prevPageBtn').disabled = currentPage <= 1;
      document.getElementById('nextPageBtn').disabled = currentPage >= totalPages;
    }}

    function changePage(delta) {{
      currentPage += delta;
      render();
      window.scrollTo({{ top: 400, behavior: 'smooth' }});
    }}

    async function openItemModal(itemId) {{
      let item = fullCatalog ? fullCatalog[itemId] : null;
      if (!item) {{
        try {{
          const shortName = itemId.replace('minecraft:', '');
          const res = await fetch(`api/item/${{shortName}}.json`);
          item = await res.json();
        }} catch(e) {{
          item = allItems.find(x => x.id === itemId);
        }}
      }}
      if (!item) return;

      document.getElementById('modalTitle').innerText = item.name.replace(/_/g, ' ');
      document.getElementById('modalId').innerText = item.id;
      document.getElementById('modalCategory').innerText = item.category;
      document.getElementById('modalImg').src = item.item_url || ('items/' + item.name + '.png');
      
      const itemUrl = item.item_url || (window.location.origin + window.location.pathname.replace(/index\\.html$/, '') + 'items/' + item.name + '.png');
      const jsonUrl = window.location.origin + window.location.pathname.replace(/index\\.html$/, '') + 'api/item/' + item.name + '.json';
      
      document.getElementById('modalItemUrl').value = itemUrl;
      document.getElementById('modalJsonUrl').value = jsonUrl;
      document.getElementById('modalEmbed').value = `<img src="${{itemUrl}}" alt="${{item.name}}" width="32" height="32">`;

      const texList = document.getElementById('modalTexturesList');
      if (item.textures && item.textures.length > 0) {{
        texList.innerHTML = item.textures.map(t => `
          <div class="texture-thumb-box">
            <img src="${{t.raw_url || t.path}}" alt="${{t.role}}">
            <span class="texture-thumb-role">${{t.role}}</span>
            <span class="texture-thumb-res">${{t.resource.replace('minecraft:', '')}}</span>
          </div>
        `).join('');
      }} else {{
        texList.innerHTML = '<span style="font-size:0.8rem; color:var(--text-muted);">No separate face textures</span>';
      }}

      document.getElementById('modalModels').innerText = (item.model_references || []).join('\\n') || 'minecraft:item/' + item.name;
      document.getElementById('itemModal').classList.add('open');
    }}

    function closeModal() {{
      document.getElementById('itemModal').classList.remove('open');
    }}

    function copyText(txt) {{
      navigator.clipboard.writeText(txt);
      showToast('Copied: ' + txt);
    }}

    function copyInput(id) {{
      const el = document.getElementById(id);
      navigator.clipboard.writeText(el.value);
      showToast('Copied to clipboard!');
    }}

    function showToast(msg) {{
      const toast = document.getElementById('toast');
      toast.innerText = msg;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2000);
    }}

    render();
  </script>
</body>
</html>
"""
    with open(index_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Web Portal index.html generated: {index_file}")


# ---------------------------------------------------------------------------
# Privacy Policy Generator (/privicy and /privacy)
# ---------------------------------------------------------------------------

def generate_privacy_policy(output_dir: Path, manifest: dict):
    version = manifest.get("version", "Latest")
    updated_at = manifest.get("updated_at", time.strftime("%Y-%m-%d"))
    base_url = manifest.get("base_url", "") or ""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Privacy Policy | Minecraft Texture API</title>
  <meta name="description" content="Privacy Policy for the open-source Minecraft Auto-Updated Texture API. Zero data collection, no cookies, no tracking.">
  <link rel="icon" href="../items/diamond.png" type="image/png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;600&family=Inter:wght@400;500;600;700;800&family=Press+Start+2P&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-dark: #0a0d12;
      --bg-card: #131720;
      --border: #232b38;
      --diamond: #2ce2e6;
      --emerald: #2ecc71;
      --gold: #f59e0b;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --code-bg: #0d1117;
      --font-pixel: 'Press Start 2P', monospace;
      --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'Fira Code', monospace;
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      background-color: var(--bg-dark);
      color: var(--text-main);
      font-family: var(--font-sans);
      line-height: 1.7;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}

    header {{
      background: linear-gradient(180deg, #11151f 0%, #0a0d12 100%);
      border-bottom: 1px solid var(--border);
      padding: 2.5rem 1.5rem 2rem;
      text-align: center;
    }}

    .back-btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      color: var(--diamond);
      text-decoration: none;
      font-size: 0.9rem;
      font-weight: 600;
      margin-bottom: 1.25rem;
      transition: opacity 0.2s;
    }}

    .back-btn:hover {{
      opacity: 0.8;
      text-decoration: underline;
    }}

    h1 {{
      font-family: var(--font-pixel);
      font-size: 1.5rem;
      color: #ffffff;
      letter-spacing: -0.5px;
      margin-bottom: 0.75rem;
      text-shadow: 0 0 20px rgba(44, 226, 230, 0.3);
    }}

    .meta-badge {{
      display: inline-block;
      padding: 0.3rem 0.8rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      background: rgba(44, 226, 230, 0.1);
      border: 1px solid rgba(44, 226, 230, 0.3);
      color: var(--diamond);
    }}

    main {{
      max-width: 820px;
      width: 100%;
      margin: 0 auto;
      padding: 2.5rem 1.5rem;
      flex: 1;
    }}

    .policy-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 2rem;
      margin-bottom: 1.5rem;
    }}

    h2 {{
      font-size: 1.25rem;
      font-weight: 700;
      color: #ffffff;
      margin-bottom: 0.75rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    h2 span.icon {{
      font-size: 1.1rem;
    }}

    p {{
      color: var(--text-muted);
      margin-bottom: 1rem;
      font-size: 0.95rem;
    }}

    p:last-child {{
      margin-bottom: 0;
    }}

    ul {{
      list-style-type: none;
      padding-left: 0;
      margin-bottom: 1rem;
    }}

    li {{
      color: var(--text-muted);
      font-size: 0.95rem;
      margin-bottom: 0.5rem;
      padding-left: 1.5rem;
      position: relative;
    }}

    li::before {{
      content: "✦";
      position: absolute;
      left: 0;
      color: var(--diamond);
      font-size: 0.8rem;
    }}

    .highlight-box {{
      background: var(--code-bg);
      border-left: 4px solid var(--emerald);
      padding: 1rem 1.25rem;
      border-radius: 0 8px 8px 0;
      margin: 1rem 0;
      font-size: 0.9rem;
      color: #e2e8f0;
    }}

    a {{
      color: var(--diamond);
      text-decoration: none;
    }}

    a:hover {{
      text-decoration: underline;
    }}

    footer {{
      border-top: 1px solid var(--border);
      padding: 2rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
      font-size: 0.85rem;
    }}
  </style>
</head>
<body>

  <header>
    <a href="../" class="back-btn">← Back to API Explorer</a>
    <h1>PRIVACY POLICY</h1>
    <div class="meta-badge">Last Updated: {updated_at} • Minecraft v{version}</div>
  </header>

  <main>
    <div class="policy-card">
      <h2><span class="icon">🛡️</span> 1. Overview & Commitment to Privacy</h2>
      <p>
        The <strong>Minecraft Auto-Updated Texture API</strong> is an open-source, public developer utility providing raw image assets, textures, and JSON metadata extracted directly from official Minecraft client JARs.
      </p>
      <div class="highlight-box">
        <strong>Our Guarantee:</strong> We do not collect, monetize, sell, or track any personal information or visitor telemetry. Your privacy is fully respected.
      </div>
    </div>

    <div class="policy-card">
      <h2><span class="icon">🚫</span> 2. No Personal Data Collection</h2>
      <p>When you query or browse this API:</p>
      <ul>
        <li><strong>No Account Required:</strong> You do not need to register, log in, or provide any email address or personal credentials.</li>
        <li><strong>No Authentication Tokens:</strong> There are no API keys, bearer tokens, or tracking IDs attached to requests.</li>
        <li><strong>No Forms or Inputs Stored:</strong> Search queries performed on the frontend are handled 100% client-side in your browser.</li>
      </ul>
    </div>

    <div class="policy-card">
      <h2><span class="icon">🍪</span> 3. Cookies and Tracking Technologies</h2>
      <p>
        This website and API do <strong>not</strong> use HTTP cookies, session storage trackers, web beacons, browser fingerprinting, or tracking pixels.
      </p>
      <p>
        We do not integrate third-party advertising networks, marketing trackers, or analytical surveillance tools (such as Google Analytics).
      </p>
    </div>

    <div class="policy-card">
      <h2><span class="icon">☁️</span> 4. Hosting & Infrastructure (GitHub Pages)</h2>
      <p>
        This API is statically hosted on <strong>GitHub Pages</strong>, a service operated by <em>GitHub, Inc.</em> (a subsidiary of Microsoft).
      </p>
      <p>
        When you send an HTTP request to any API endpoint (such as <code>/api/items.json</code> or <code>/items/diamond.png</code>), GitHub's network edge servers may process standard technical request information (including IP address, browser User-Agent, and timestamp) for the purposes of:
      </p>
      <ul>
        <li>DDoS protection, bot filtering, and network defense</li>
        <li>CDN edge caching and high-availability delivery</li>
        <li>Aggregated traffic monitoring as required for platform security</li>
      </ul>
      <p>
        For further details regarding GitHub's data processing practices, please refer to the <a href="https://docs.github.com/en/site-policy/privacy-policies/github-privacy-statement" target="_blank" rel="noopener noreferrer">GitHub Privacy Statement</a>. The creators of this API have no access to raw access log data.
      </p>
    </div>

    <div class="policy-card">
      <h2><span class="icon">⚖️</span> 5. Intellectual Property & Fair Use</h2>
      <p>
        Minecraft game content, assets, and textures are copyright and trademarks of <strong>Mojang Studios / Microsoft Corporation</strong>.
      </p>
      <p>
        This project is an unofficial community developer resource provided under fair use principles for modders, bot creators, dashboard developers, and hobbyists.
      </p>
    </div>

    <div class="policy-card">
      <h2><span class="icon">📦</span> 6. Open Source Transparency</h2>
      <p>
        The complete automated pipeline, source code, and deployment workflows for this project are fully open source and verifiable on GitHub:
      </p>
      <p>
        <a href="https://github.com/Project-crafting/minecraft-auto-updated-textures-api" target="_blank" rel="noopener noreferrer">
          👉 View Repository on GitHub (Project-crafting/minecraft-auto-updated-textures-api)
        </a>
      </p>
    </div>
  </main>

  <footer>
    <p>
      Minecraft assets and textures are the property of <strong>Mojang Studios / Microsoft</strong>.
    </p>
    <p style="margin-top:0.5rem;">
      <a href="../">API Home</a> • <a href="./">Privacy Policy</a>
    </p>
  </footer>

</body>
</html>
"""

    # Generate both routes so that /privicy, /privacy, /privicy.html, /privacy.html work seamlessly!
    routes = [
        output_dir / "privicy" / "index.html",
        output_dir / "privacy" / "index.html",
        output_dir / "privicy.html",
        output_dir / "privacy.html",
    ]

    for route_file in routes:
        route_file.parent.mkdir(parents=True, exist_ok=True)
        with open(route_file, "w", encoding="utf-8") as f:
            f.write(html_content)

    print(f"Privacy Policy pages generated at /privicy and /privacy")


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Minecraft Auto-Updated Texture API Builder"
    )
    parser.add_argument(
        "--version",
        default="latest",
        help="Target Minecraft version or 'latest' (default: latest)",
    )
    parser.add_argument(
        "--channel",
        choices=["release", "snapshot"],
        default="release",
        help="Release channel: release or snapshot (default: release)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL", os.environ.get("GITHUB_PAGES_BASE_URL", "")),
        help="Base URL for public images and endpoints (e.g. https://<user>.github.io/<repo>)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to place generated static site and API (default: ./public)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for caching downloaded JARs and extracted assets (default: ./.cache)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild even if version has not changed",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check if a new version is available without building",
    )

    args = parser.parse_args()

    try:
        sys.exit(
            run_pipeline(
                version=args.version,
                channel=args.channel,
                base_url=args.base_url,
                output_dir=args.output_dir,
                cache_dir=args.cache_dir,
                force=args.force,
                check_only=args.check_only,
            )
        )
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
    except Exception as e:
        print(f"\n[FATAL ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
