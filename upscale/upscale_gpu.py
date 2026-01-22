import argparse
import re
from pathlib import Path
from subprocess import run
from typing import Optional, List
from PIL import Image
from tqdm import tqdm

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# ---------- FS helpers ----------
def find_or_make_dirs(explicit_input: Optional[Path]) -> tuple[Path, Path, Path]:
    base = Path(__file__).resolve().parent
    if explicit_input:
        inp = explicit_input
    else:
        cwd = Path.cwd()
        has_imgs = any(p.is_file() and p.suffix.lower() in SUPPORTED_EXTS for p in cwd.iterdir())
        inp = cwd if (has_imgs or cwd.name.lower() in ("обои", "wallpapers", "wallpaper")) else (base / "Обои")
    out = inp / "upscale"
    tools = base / "tools"
    out.mkdir(parents=True, exist_ok=True)
    tools.mkdir(parents=True, exist_ok=True)
    return inp, out, tools

def list_images(folder: Path):
    return [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]

def find_exe(base_dir: Path, tools_dir: Path) -> Optional[Path]:
    for cand in [tools_dir / "realesrgan-ncnn-vulkan.exe",
                 base_dir / "realesrgan-ncnn-vulkan.exe",
                 next((p for p in tools_dir.rglob("realesrgan-ncnn-vulkan.exe")), None)]:
        if isinstance(cand, Path) and cand and cand.exists():
            return cand
    return None

def find_models_dir(exe_path: Optional[Path], tools_dir: Path) -> Optional[Path]:
    if exe_path:
        near = exe_path.parent / "models"
        if near.exists() and any(near.glob("*.param")) and any(near.glob("*.bin")):
            return near
    models = tools_dir / "models"
    if models.exists() and any(models.glob("*.param")) and any(models.glob("*.bin")):
        return models
    nested = next((p for p in tools_dir.rglob("models") if p.is_dir() and any(p.glob("*.param")) and any(p.glob("*.bin"))), None)
    return nested

# ---------- Model selection ----------
def list_available_model_names(models_dir: Path) -> List[str]:
    names = set()
    for p in models_dir.glob("*.param"):
        if (models_dir / f"{p.stem}.bin").exists():
            names.add(p.stem)
    return sorted(names, key=str.lower)

def parse_scale_from_name(name: str) -> Optional[int]:
    s = name.lower()
    # RealESRGAN_General_x4_v3, realesr-animevideov3-x3, 4xLSDIR...
    m = re.search(r'[_\-]x([234])(?:[_\-]|$)', s)
    if m:
        return int(m.group(1))
    m2 = re.match(r'([234])x', s)
    if m2:
        return int(m2.group(1))
    return None

def pick_best_model_for_scale(available: List[str], mode: str, want_scale: int) -> Optional[str]:
    # Сначала точное совпадение масштаба
    def has_exact(scale: int, pool: List[str]) -> Optional[str]:
        for n in pool:
            if parse_scale_from_name(n) == scale:
                return n
        return None

    if mode == "anime":
        # приоритеты среди аниме-весов
        anime_pool = [n for n in available if "anime" in n.lower()]
        chosen = has_exact(want_scale, anime_pool)
        if chosen:
            return chosen
        for pref in ["realesr-animevideov3-x4", "realesr-animevideov3-x3", "realesr-animevideov3-x2"]:
            for n in anime_pool:
                if pref in n.lower():
                    return n
        # если аниме не нашлись — свалимся в общие
    # photo/auto
    # популярные general
    general_pool = [n for n in available if "general" in n.lower()]
    chosen = has_exact(want_scale, general_pool)
    if chosen:
        return chosen
    for pref in ["realesrgan_general_wdn_x4_v3", "realesrgan_general_x4_v3"]:
        for n in general_pool:
            if pref == n.lower():
                return n
    # семейства 4x...
    other_pool = [n for n in available if n not in general_pool]
    chosen = has_exact(want_scale, other_pool)
    if chosen:
        return chosen
    # любые подходящие 4x-модели
    for pref in ["4xlsdirplusc", "4xlsdircompactc3", "4xlsdir", "4xhfa2k", "4xnomos8ksc", "4xnmkd-superscale-sp_178000_g", "4xnmkd-siax_200k", "uniscale_restore"]:
        for n in other_pool:
            if pref == n.lower():
                return n
    # последний шанс — первая доступная
    return available[0] if available else None

# ---------- Runner & post-process ----------
def run_realesrgan(exe: Path, models_dir: Path, model_name: str,
                   inp: Path, out: Path, scale: int, tile: int, jobs: str, gpu: int) -> int:
    cmd = [str(exe), "-m", str(models_dir), "-n", model_name,
           "-i", str(inp), "-o", str(out), "-s", str(scale),
           "-f", "jpg", "-t", str(tile), "-j", jobs, "-g", str(gpu)]
    print("Команда:", " ".join(cmd))
    proc = run(cmd)
    return proc.returncode

def rescale_outputs_to_requested(out_dir: Path, orig_dir: Path, run_scale: int, want_scale: int):
    """Если модель не совпадает по масштабу: приводим результат к нужному масштабу."""
    if run_scale == want_scale:
        return
    ratio = want_scale / run_scale
    outs = [p for p in sorted(out_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".png", ".webp"}]
    if not outs:
        return
    # аккуратно пересэмплируем
    for p in tqdm(outs, desc=f"Resample {run_scale}x→{want_scale}x"):
        try:
            im = Image.open(p).convert("RGB")
            w, h = im.size
            new_w = max(1, int(round(w * ratio)))
            new_h = max(1, int(round(h * ratio)))
            im2 = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            im2.save(p, quality=95)
        except Exception as e:
            print(f"  Ошибка ресэмплинга {p.name}: {e}")

def rename_outputs_sequential(out: Path):
    files = sorted([p for p in out.iterdir() if p.is_file() and p.suffix.lower() in {".jpg", ".png", ".webp"}])
    if not files:
        return
    tmp = []
    for i, p in enumerate(files, start=1):
        t = out / f"__tmp__{i}{p.suffix.lower()}"
        p.rename(t); tmp.append(t)
    for i, p in enumerate(sorted(tmp), start=1):
        p.rename(out / f"upscale-{i}{p.suffix.lower()}")

# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser(description="Upscale c realesrgan-ncnn-vulkan + кастомные модели Upscayl (без артефактов тайлов).")
    parser.add_argument("--scale", type=int, choices=[2,3,4], default=3, help="Желаемый масштаб x2/x3/x4.")
    parser.add_argument("--model", choices=["auto","photo","anime"], default="auto", help="Тип контента для выбора модели.")
    parser.add_argument("--model-name", type=str, default=None, help="Точное имя модели (как файл .param/.bin без расширений).")
    parser.add_argument("--tile", type=int, default=200, help="Размер тайла (уменьши при нехватке VRAM).")
    parser.add_argument("--jobs", type=str, default="4:4:4", help="Потоки load:proc:save (напр. 2:2:2).")
    parser.add_argument("--gpu", type=int, default=0, help="GPU индекс.")
    parser.add_argument("--limit", type=int, default=0, help="Обработать только N первых файлов.")
    parser.add_argument("--input", type=str, default=None, help="Путь к папке с исходниками.")
    args = parser.parse_args()

    explicit_input = Path(args.input) if args.input else None
    inp, out, tools = find_or_make_dirs(explicit_input)
    base = Path(__file__).resolve().parent

    imgs = list_images(inp)
    if args.limit and args.limit > 0:
        imgs = imgs[:args.limit]
    if not imgs:
        print(f"В {inp} нет изображений.")
        return

    exe = find_exe(base, tools)
    if not exe:
        print("❗ Не найден realesrgan-ncnn-vulkan.exe (ожидается tools\\ или рядом со скриптом).")
        return

    models_dir = find_models_dir(exe, tools)
    if not models_dir:
        print("❗ Не найдена папка models с .param/.bin в tools\\.")
        return

    available = list_available_model_names(models_dir)
    if not available:
        print(f"В {models_dir} нет пар .param/.bin.")
        return

    # Выбор модели
    if args.model_name:
        chosen = args.model_name
        if chosen not in available:
            print(f"⚠️ Модель '{chosen}' не найдена. Доступно:\n- " + "\n- ".join(available))
            return
    else:
        chosen = pick_best_model_for_scale(available, args.model, args.scale)
        if not chosen:
            print("Не удалось выбрать модель. Доступно:\n- " + "\n- ".join(available))
            return

    model_scale = parse_scale_from_name(chosen)
    # ОБЯЗАТЕЛЬНО: запускать движок в масштабе самой модели (иначе будут квадраты!)
    run_scale = model_scale if model_scale else args.scale

    print("==== НАСТРОЙКИ ====")
    print("EXE      :", exe)
    print("MODELS   :", models_dir)
    print("MODEL    :", chosen, f"(model_scale={model_scale or 'unknown'})")
    print("RUN-SCALE:", f"x{run_scale}", " | REQUESTED:", f"x{args.scale}")
    print("INPUT    :", inp)
    print("OUTPUT   :", out)
    print("TILE/JOBS:", f"{args.tile} / {args.jobs}")
    print("GPU      :", args.gpu)
    print("===================")

    rc = run_realesrgan(exe, models_dir, chosen, inp, out, run_scale, args.tile, args.jobs, args.gpu)
    if rc != 0:
        print("❌ Real-ESRGAN вернул ошибку. Попробуй меньше --tile (например, 100) или другой --gpu.")
        return

    # Если пользователь хотел другой масштаб — приводим результат к нему без швов
    rescale_outputs_to_requested(out, inp, run_scale, args.scale)

    rename_outputs_sequential(out)
    print(f"✅ Готово. Результаты: {out}")

if __name__ == "__main__":
    main()
