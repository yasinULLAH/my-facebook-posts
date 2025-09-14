#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import csv
import shutil
from pathlib import Path
from urllib.parse import urlparse

# --- Dependency Installation Check ---
try:
    import requests
    import yt_dlp
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    from tqdm import tqdm
except ImportError as e:
    print(f"❌ Missing required library: {e.name}", file=sys.stderr)
    print("Please install the required libraries by running: pip install requests yt-dlp Pillow arabic_reshaper python-bidi tqdm", file=sys.stderr)
    sys.exit(1)

# --- Constants ---
DEFAULT_OUTDIR = Path("./shorts_output")
DEFAULT_SIZE = "1080x1920"
DEFAULT_THUMB_BG = "#101114"
DEFAULT_MARGIN = 40
DEFAULT_MAX_LINES = 2
MAX_CAPTION_WIDTH_RATIO = 0.9

# --- Utility Functions ---
def check_dependencies():
    """Checks if ffmpeg and ffprobe are installed and in the system's PATH."""
    if not shutil.which("ffmpeg"):
        print("❌ ERROR: `ffmpeg` is not installed or not found in your system's PATH.", file=sys.stderr); sys.exit(1)
    if not shutil.which("ffprobe"):
        print("❌ ERROR: `ffprobe` is not installed or not found in your system's PATH.", file=sys.stderr); sys.exit(1)

def slugify(text: str) -> str:
    """Create a filesystem-safe slug from a string."""
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    text = re.sub(r'[\s_-]+', '-', text)
    return text[:80]

def run_command(command: list[str], description: str, cwd: Path | None = None) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8', cwd=cwd)
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR in '{description}': Command failed.", file=sys.stderr)
        print(f"   Command: {' '.join(command)}", file=sys.stderr)
        print(f"   Stderr:\n{e.stderr}", file=sys.stderr)
        raise

def find_windows_font(font_names: list[str]) -> Path | None:
    if sys.platform != "win32": return None
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if not font_dir.is_dir(): return None
    for font_name in font_names:
        font_path = font_dir / font_name
        if font_path.is_file(): return font_path
    return None

def get_media_info(media_path: Path) -> dict:
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_type", "-of", "json", str(media_path)]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        streams = json.loads(result.stdout).get("streams", [])
        return streams[0] if streams else {"codec_type": "audio"}
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return {"codec_type": "audio"}

def escape_ffmpeg_path(path: Path) -> str:
    path_str = str(path.resolve())
    if sys.platform == "win32":
        return path_str.replace('\\', '/').replace(':', '\\:')
    return path_str

def wrap_srt_text(srt_content: str, font_path: Path, font_size: int, max_width: int) -> str:
    try: font = ImageFont.truetype(str(font_path), font_size)
    except IOError: print(f"⚠️ Warning: Could not load font {font_path} for wrapping.", file=sys.stderr); return srt_content
    srt_blocks = re.split(r'(\r?\n\s*\r?\n)', srt_content); wrapped_content = ""
    for i in range(0, len(srt_blocks), 2):
        block = srt_blocks[i]; separator = srt_blocks[i+1] if i+1 < len(srt_blocks) else ""
        lines = block.strip().split('\n')
        if len(lines) < 2 or '-->' not in lines[1]: wrapped_content += block + separator; continue
        header = '\n'.join(lines[:2]); text_lines = [line.strip() for line in lines[2:]]; wrapped_text_lines = []
        for line in text_lines:
            processed_line = process_rtl_text(line)
            if font.getlength(processed_line) <= max_width: wrapped_text_lines.append(line)
            else:
                words = line.split(' '); current_line = ""
                for word in words:
                    if not current_line: current_line = word
                    else:
                        test_line = f"{current_line} {word}"
                        if font.getlength(process_rtl_text(test_line)) <= max_width: current_line = test_line
                        else: wrapped_text_lines.append(current_line); current_line = word
                if current_line: wrapped_text_lines.append(current_line)
        wrapped_content += header + '\n' + '\n'.join(wrapped_text_lines) + separator
    return wrapped_content

def prepare_media_source(media_input: str, temp_dir: Path) -> Path:
    parsed_url = urlparse(media_input)
    if parsed_url.scheme in ('http', 'https') and parsed_url.netloc:
        if 'youtube.com' in parsed_url.netloc or 'youtu.be' in parsed_url.netloc:
            print(f"🌐 Downloading YouTube video: {media_input}")
            ydl_opts = {'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', 'outtmpl': str(temp_dir / '%(id)s.%(ext)s'), 'quiet': True, 'noprogress': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(media_input, download=True); print("✅ YouTube download complete.")
                return Path(ydl.prepare_filename(info))
        else:
            print(f"🌐 Downloading direct media link: {media_input}")
            try:
                with requests.get(media_input, stream=True) as r:
                    r.raise_for_status(); ext = Path(parsed_url.path).suffix or '.tmp'; temp_file_path = temp_dir / f"downloaded{ext}"
                    with open(temp_file_path, 'wb') as f, tqdm(desc="Downloading", total=int(r.headers.get('content-length', 0)), unit='iB', unit_scale=True, unit_divisor=1024) as bar:
                        for chunk in r.iter_content(chunk_size=8192): bar.update(f.write(chunk))
                    return temp_file_path
            except requests.exceptions.RequestException as e: print(f"❌ Failed to download direct URL: {e}", file=sys.stderr); sys.exit(1)
    else:
        local_path = Path(media_input)
        if not local_path.is_file(): print(f"❌ Local media file not found: {media_input}", file=sys.stderr); sys.exit(1)
        return local_path

def process_rtl_text(text: str) -> str:
    reshaped_text = arabic_reshaper.reshape(text)
    return get_display(reshaped_text)

def generate_thumbnail(title: str, direction: str, bg_color: str, font_path: Path, size: tuple[int, int], margin: int, max_lines: int, out_path: Path):
    width, height = size; image = Image.new("RGB", (width, height), bg_color); draw = ImageDraw.Draw(image)
    font_size = int(height / 12); font = ImageFont.truetype(str(font_path), font_size)
    processed_title = process_rtl_text(title) if direction == "rtl" else title
    words = processed_title.split(); lines = []; current_line = ""
    for word in words:
        if not current_line: current_line = word
        else:
            new_line = f"{current_line} {word}";
            if font.getlength(new_line) <= width - 2 * margin: current_line = new_line
            else: lines.append(current_line); current_line = word
    if current_line: lines.append(current_line)
    while len(lines) > max_lines and font_size > 10:
        font_size -= 2; font = ImageFont.truetype(str(font_path), font_size); lines = []; current_line = ""
        for word in words:
            if not current_line: current_line = word
            else:
                new_line = f"{current_line} {word}"
                if font.getlength(new_line) <= width - 2 * margin: current_line = new_line
                else: lines.append(current_line); current_line = word
        if current_line: lines.append(current_line)
    total_text_height = sum(font.getbbox(line)[3] for line in lines); y_start = (height - total_text_height) / 2
    for line in lines:
        text_width = font.getlength(line); x = (width - text_width) / 2
        draw.text((x, y_start), line, font=font, fill="white", align="center"); y_start += font.getbbox(line)[3]
    image.save(out_path, "JPEG")

def process_short(short_data: dict, source_media_path_orig: Path, media_info: dict, font_calibri: Path, font_urdu: Path, args: argparse.Namespace) -> dict | None:
    short_id, title_slug = short_data['id'], slugify(short_data['title'])
    final_output_video_path = (args.outdir / f"{short_id}__{title_slug}.mp4").resolve()
    final_output_srt_path = args.outdir / f"{short_id}__{title_slug}.srt"
    final_output_thumb_path = args.outdir / f"{short_id}__thumb.jpg"
    
    duration = short_data['duration_sec']
    if not (5.0 <= duration <= 60.0):
        print(f"⚠️ Skipping short '{short_id}': Duration ({duration:.2f}s) outside 5-60s range.", file=sys.stderr)
        return None
    
    # This part is kept: The script will still generate your .srt files
    srt_content = short_data.get('srt')
    if srt_content:
        final_output_srt_path.write_text(srt_content, encoding="utf-8")
    
    with tempfile.TemporaryDirectory() as temp_work_dir_str:
        temp_work_dir = Path(temp_work_dir_str)
        safe_source_path = temp_work_dir / f"source{source_media_path_orig.suffix}"
        shutil.copy(source_media_path_orig, safe_source_path)
        width, height = map(int, args.size.split('x'))

        if media_info.get("codec_type") == "audio":
            # --- Generates video from audio, WITHOUT subtitles ---
            safe_thumb_path = temp_work_dir / "thumb.jpg"
            font_for_thumb = font_urdu if short_data.get("direction") == "rtl" else font_calibri
            generate_thumbnail(short_data['thumbnail_title'], short_data['direction'], args.thumb_bg, font_for_thumb, (width, height), args.margin, args.max_lines, safe_thumb_path)
            
            filter_chain = [
                f"[0:v]scale={width}:{height},loop=loop=-1:size=1:start=0[bg]",
                f"[1:a]atrim=start={short_data['start_sec']}:end={short_data['end_sec']},asetpts=PTS-STARTPTS[a_trimmed]",
                "[a_trimmed]asplit=2[a][a_wave]",
                f"[a_wave]showwaves=s={width}x{int(height*0.2)}:mode=line:colors=#FFFFFF|#CCCCCC:rate=25,format=yuva420p[wave]",
                "[bg][wave]overlay=(W-w)/2:H*0.65[v_final]",
                "[a]loudnorm=I=-14:LRA=11:TP=-1.5[a_final]"
            ]
            
            command = ["ffmpeg", "-y", "-i", str(safe_thumb_path), "-i", str(safe_source_path), "-filter_complex", ";".join(filter_chain), "-map", "[v_final]", "-map", "[a_final]", "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", "-b:a", "128k", "-t", str(duration), str(final_output_video_path)]
            run_command(command, f"Generating audio short for '{short_data['title']}'", cwd=temp_work_dir)
            shutil.copy(safe_thumb_path, final_output_thumb_path)

        else: # Video Source
            # --- Generates video from video, WITHOUT subtitles ---
            video_filters = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            audio_filters = "loudnorm=I=-14:LRA=11:TP=-1.5"

            command = ["ffmpeg", "-y", "-ss", str(short_data['start_sec']), "-to", str(short_data['end_sec']), "-i", str(safe_source_path), "-vf", video_filters, "-af", audio_filters, "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", "-b:a", "128k", str(final_output_video_path)]
            run_command(command, f"Generating video short for '{short_data['title']}'", cwd=temp_work_dir)
            
            thumb_command = ["ffmpeg", "-y", "-ss", "0", "-i", str(final_output_video_path), "-vframes", "1", "-q:v", "2", str(final_output_thumb_path)]
            run_command(thumb_command, f"Generating thumbnail for '{short_data['title']}'")

    return {"id": short_id, "title": short_data['title'], "start_sec": short_data['start_sec'], "end_sec": short_data['end_sec'], "duration_sec": duration, "language": short_data['language'], "direction": short_data['direction'], "category": short_data['category'], "keywords": ",".join(short_data['keywords']), "video_path": final_output_video_path.name, "srt_path": final_output_srt_path.name if srt_content else "", "thumb_path": final_output_thumb_path.name}

def main():
    check_dependencies()
    parser = argparse.ArgumentParser(description="Create YouTube Shorts from a JSON plan and a media file.")
    parser.add_argument("--plan", type=Path, required=True, help="Path to the JSON plan file.")
    parser.add_argument("--media", type=str, required=True, help="Path or URL to the source media file.")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR, help=f"Output directory. Default: {DEFAULT_OUTDIR}")
    parser.add_argument("--burn_captions", action=argparse.BooleanOptionalAction, default=True, help="Burn captions into the video. Default: on.")
    parser.add_argument("--font_calibri", type=Path, help="Path to a .ttf font for English captions (optional, auto-detected on Windows).")
    parser.add_argument("--font_urdu", type=Path, help="Path to a .ttf font for Urdu/Arabic captions (optional, auto-detected on Windows).")
    parser.add_argument("--thumb_bg", type=str, default=DEFAULT_THUMB_BG, help=f"Background color for audio-only thumbnails. Default: {DEFAULT_THUMB_BG}")
    parser.add_argument("--size", type=str, default=DEFAULT_SIZE, help=f"Output resolution (e.g., 1080x1920). Default: {DEFAULT_SIZE}")
    parser.add_argument("--margin", type=int, default=DEFAULT_MARGIN, help=f"Vertical margin for captions and thumbnails. Default: {DEFAULT_MARGIN}")
    parser.add_argument("--max_lines", type=int, default=DEFAULT_MAX_LINES, help=f"Max lines for thumbnail titles. Default: {DEFAULT_MAX_LINES}")
    args = parser.parse_args()

    if not args.plan.is_file(): print(f"❌ Plan file not found: {args.plan}", file=sys.stderr); sys.exit(1)
    args.outdir.mkdir(parents=True, exist_ok=True)
    print(f"📂 Output directory: {args.outdir.resolve()}")
    with open(args.plan, "r", encoding="utf-8") as f: plan_data = json.load(f)

    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        source_media_path = prepare_media_source(args.media, temp_dir)
        media_info = get_media_info(source_media_path)
        
        font_calibri_path, font_urdu_path = args.font_calibri, args.font_urdu
        if not font_calibri_path:
            font_calibri_path = find_windows_font(['calibri.ttf', 'arial.ttf'])
            if font_calibri_path: print(f"ℹ️ Automatically found LTR font: {font_calibri_path}")
        if not font_urdu_path:
            font_urdu_path = find_windows_font(['tahoma.ttf', 'arial.ttf'])
            if font_urdu_path: print(f"ℹ️ Automatically found RTL font: {font_urdu_path}")

        needs_fonts = args.burn_captions or media_info.get("codec_type") == "audio"
        if needs_fonts:
            if not font_calibri_path or not font_calibri_path.is_file(): print("❌ English/LTR font not found or specified. Use --font_calibri.", file=sys.stderr); sys.exit(1)
            if not font_urdu_path or not font_urdu_path.is_file(): print("❌ Urdu/RTL font not found or specified. Use --font_urdu.", file=sys.stderr); sys.exit(1)

        manifest_data = []
        shorts_list = plan_data.get("shorts", [])
        if not shorts_list: print("⚠️ No shorts found in the plan file.", file=sys.stderr); return

        progress_bar = tqdm(shorts_list, unit="short", desc="Processing shorts")
        for short_data in progress_bar:
            progress_bar.set_postfix(title=short_data.get('title', 'N/A')[:30])
            try:
                result = process_short(short_data, source_media_path, media_info, font_calibri_path, font_urdu_path, args)
                if result: manifest_data.append(result)
            except Exception as e:
                print(f"\n❌ An unexpected error occurred while processing short '{short_data.get('id')}': {e}", file=sys.stderr)
                print("   Skipping this short and continuing...", file=sys.stderr)

    if manifest_data:
        manifest_path = args.outdir / "manifest.csv"
        with open(manifest_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=manifest_data[0].keys())
            writer.writeheader(); writer.writerows(manifest_data)
        print(f"📊 Wrote manifest file: {manifest_path.name}")
    
    print("\n🎉 All shorts processed successfully!")

if __name__ == "__main__":
    main()