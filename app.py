from flask import Flask, request, jsonify, send_file
import subprocess
import requests
import os
import tempfile
import threading
import uuid
import shutil
import sys
from openai import OpenAI

sys.stdout.reconfigure(line_buffering=True)

app = Flask(__name__)
jobs = {}

# Lazy OpenAI client — initialized on first use, not at startup
openai_client = None

def get_openai_client():
    global openai_client
    if openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise Exception("OPENAI_API_KEY environment variable is not set in Railway")
        openai_client = OpenAI(api_key=api_key)
    return openai_client


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def download_file(url, dest_path):
    import time
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Cache-Control': 'no-cache, no-store',
        'Pragma': 'no-cache'
    }
    separator = '&' if '?' in url else '?'
    url = f"{url}{separator}t={int(time.time() * 1000)}"
    response = session.get(url, headers=headers, stream=True, allow_redirects=True)
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            url = url + f"&confirm={value}"
            response = session.get(url, headers=headers, stream=True, allow_redirects=True)
            break
    with open(dest_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)
    file_size = os.path.getsize(dest_path)
    print(f"Downloaded {url[:80]}... → {file_size} bytes")
    return dest_path


def get_audio_duration(audio_path):
    result = subprocess.run([
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        audio_path
    ], capture_output=True, text=True)
    output = result.stdout.strip()
    if not output or output == 'N/A':
        file_size = os.path.getsize(audio_path)
        return max(5.0, file_size / 16000)
    return float(output)


def ms_to_srt_time(ms: float) -> str:
    """Convert milliseconds to SRT timestamp HH:MM:SS,mmm"""
    ms = int(ms)
    hours = ms // 3_600_000
    ms %= 3_600_000
    minutes = ms // 60_000
    ms %= 60_000
    seconds = ms // 1_000
    millis = ms % 1_000
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def transcribe_audio_to_srt_blocks(audio_path: str, start_offset_sec: float) -> list:
    """
    Send audio to OpenAI Whisper API with verbose_json to get segments with timestamps.
    Returns list of { start_ms, end_ms, text } dicts offset by start_offset_sec.
    """
    print(f"  [Whisper] Transcribing {os.path.basename(audio_path)} (offset: {start_offset_sec:.2f}s)...")
    client = get_openai_client()
    with open(audio_path, 'rb') as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            prompt="Transcribe in Roman/Latin script only. Write Hindi words as they sound in English letters. Example: Namaste dosto, aaj hum baat karenge"
        )

    blocks = []
    for segment in response.segments:
        blocks.append({
            'start_ms': (start_offset_sec + segment.start) * 1000,
            'end_ms':   (start_offset_sec + segment.end) * 1000,
            'text':     segment.text.strip()
        })

    print(f"  [Whisper] Got {len(blocks)} segments")
    return blocks


def build_srt(all_blocks: list) -> str:
    """Convert list of { start_ms, end_ms, text } into SRT string."""
    srt_lines = []
    for i, block in enumerate(all_blocks, start=1):
        srt_lines.append(str(i))
        srt_lines.append(f"{ms_to_srt_time(block['start_ms'])} --> {ms_to_srt_time(block['end_ms'])}")
        srt_lines.append(block['text'])
        srt_lines.append("")
    return "\n".join(srt_lines)


def burn_subtitles(video_path: str, srt_path: str, output_path: str):
    """Burn SRT subtitles into video using FFmpeg with Hindi font support."""

    # Use drawtext-friendly approach: pass font file directly via ASS override
    font_path = "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"
    if not os.path.exists(font_path):
        print(f"WARNING: Font not found at {font_path}, trying fallback...")
        font_path = None

    # Build the subtitles filter
    if font_path:
        # Copy font to same dir as SRT so libass can find it easily
        font_dir = os.path.dirname(srt_path)
        font_copy = os.path.join(font_dir, "NotoSansDevanagari-Regular.ttf")
        shutil.copy(font_path, font_copy)
        vf = (
            f"subtitles={srt_path}:fontsdir={font_dir}:force_style="
            "'FontName=Noto Sans Devanagari"
            ",FontSize=18"
            ",PrimaryColour=&H00FFFFFF"
            ",OutlineColour=&H00000000"
            ",BackColour=&H80000000"
            ",Outline=2"
            ",Shadow=1"
            ",Alignment=2"
            ",MarginV=30'"
        )
    else:
        vf = (
            f"subtitles={srt_path}:force_style="
            "'FontSize=18"
            ",PrimaryColour=&H00FFFFFF"
            ",OutlineColour=&H00000000"
            ",Outline=2"
            ",Alignment=2"
            ",MarginV=30'"
        )

    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-vf', vf,
        '-c:a', 'copy',
        output_path
    ]

    print(f"[Subtitle] Running FFmpeg with vf: {vf[:120]}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Log full stderr for easier debugging
        print(f"[Subtitle] FULL STDERR:\n{result.stderr}")
        raise Exception(f"Subtitle burn failed: {result.stderr[-800:]}")
    print(f"Subtitles burned → {os.path.getsize(output_path)} bytes")
    return output_path


# ─────────────────────────────────────────────
#  SCENE PROCESSING
# ─────────────────────────────────────────────

def create_scene_video(scene, temp_dir, scene_index):
    import time

    # ── 1. Download audio ──────────────────────────────────────────
    audio_id = scene['audioFileId']
    audio_url = (
        f"https://drive.usercontent.google.com/download"
        f"?id={audio_id}&export=download&confirm=t&uuid={uuid.uuid4()}&t={int(time.time())}"
    )
    audio_path = os.path.join(temp_dir, f"audio_{scene_index}_{audio_id}.mp3")
    print(f"[Scene {scene_index}] Downloading audio ID: {audio_id}")
    download_file(audio_url, audio_path)

    audio_size = os.path.getsize(audio_path)
    print(f"[Scene {scene_index}] Audio size: {audio_size} bytes")
    if audio_size < 1000:
        raise Exception(f"Audio too small: {audio_size} bytes - likely wrong file")

    with open(audio_path, 'rb') as f:
        header = f.read(10)
    print(f"[Scene {scene_index}] Audio header: {header[:4].hex()}")
    if b'<' in header[:5]:
        raise Exception(f"Audio download returned HTML, not MP3. Header: {header}")

    # ── 2. Get duration ────────────────────────────────────────────
    duration = get_audio_duration(audio_path)
    print(f"[Scene {scene_index}] Duration: {duration}s")
    if duration < 0.5:
        raise Exception(f"Duration too short: {duration}s")

    # ── 3. Download images ─────────────────────────────────────────
    images = scene.get('images', [])
    num_images = len(images)
    time_per_image = round(duration / num_images, 3)
    print(f"[Scene {scene_index}] {num_images} images, {time_per_image}s each")

    image_paths = []
    for i, img_url in enumerate(images):
        img_path = os.path.join(temp_dir, f"img_{scene_index}_{i}.jpg")
        download_file(img_url, img_path)
        img_size = os.path.getsize(img_path)
        print(f"[Scene {scene_index}] Image {i}: {img_size} bytes")
        if img_size < 500:
            raise Exception(f"Image {i} too small: {img_size} bytes")
        image_paths.append(img_path)

    # ── 4. Create individual image clips ──────────────────────────
    clip_paths = []
    for i, img_path in enumerate(image_paths):
        clip_path = os.path.join(temp_dir, f"clip_{scene_index}_{i}.mp4")
        cmd = [
            'ffmpeg', '-y',
            '-loop', '1',
            '-i', img_path,
            '-t', str(time_per_image),
            '-vf', (
                'scale=1280:720:'
                'force_original_aspect_ratio=decrease,'
                'pad=1280:720:(ow-iw)/2:(oh-ih)/2,'
                'setsar=1'
            ),
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-tune', 'stillimage',
            '-crf', '28',
            '-pix_fmt', 'yuv420p',
            '-r', '24',
            '-an',
            clip_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Clip {i} failed: {result.stderr[-300:]}")
        print(f"[Scene {scene_index}] Clip {i}: {os.path.getsize(clip_path)} bytes")
        clip_paths.append(clip_path)

    # ── 5. Concat image clips into silent video ────────────────────
    concat_file = os.path.join(temp_dir, f"concat_{scene_index}.txt")
    with open(concat_file, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{os.path.abspath(cp)}'\n")

    silent_video = os.path.join(temp_dir, f"silent_{scene_index}.mp4")
    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_file,
        '-c', 'copy', '-an',
        silent_video
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Silent concat failed: {result.stderr[-300:]}")

    # ── 6. Merge video + audio ─────────────────────────────────────
    scene_output = os.path.join(temp_dir, f"scene_{scene_index}.mp4")
    cmd = [
        'ffmpeg', '-y',
        '-i', silent_video,
        '-i', audio_path,
        '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest',
        scene_output
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Audio merge failed: {result.stderr[-400:]}")

    scene_size = os.path.getsize(scene_output)
    print(f"[Scene {scene_index}] Final scene: {scene_size} bytes")
    if scene_size < 1000:
        raise Exception(f"Scene output empty: {scene_size} bytes")

    # ── 7. Cleanup intermediates ───────────────────────────────────
    for cp in clip_paths:
        if os.path.exists(cp): os.remove(cp)
    if os.path.exists(silent_video): os.remove(silent_video)
    if os.path.exists(concat_file): os.remove(concat_file)

    # Return scene path + duration + audio path (audio kept for Whisper)
    return scene_output, duration, audio_path


# ─────────────────────────────────────────────
#  CONCATENATION
# ─────────────────────────────────────────────

def concatenate_scenes(scene_videos, output_path, temp_dir):
    print("=== Concatenating scenes ===")
    for i, vp in enumerate(scene_videos):
        print(f"  Scene {i}: {os.path.getsize(vp)} bytes")

    concat_file = os.path.join(temp_dir, 'final_concat.txt')
    with open(concat_file, 'w') as f:
        for vp in scene_videos:
            f.write(f"file '{os.path.abspath(vp)}'\n")

    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_file,
        '-c:v', 'libx264', '-preset', 'ultrafast',
        '-crf', '28', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k',
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Concat failed: {result.stderr[-500:]}")

    print(f"Final video: {os.path.getsize(output_path)} bytes")
    return output_path


# ─────────────────────────────────────────────
#  JOB PROCESSOR
# ─────────────────────────────────────────────

def process_video_job(job_id, scenes):
    temp_dir = tempfile.mkdtemp(prefix=f"job_{job_id}_")
    try:
        jobs[job_id]['status'] = 'processing'
        print(f"Job {job_id}: Total scenes = {len(scenes)}")

        for i, scene in enumerate(scenes):
            print(f"  Scene {i}: audioFileId={scene.get('audioFileId')} sceneIndex={scene.get('sceneIndex')}")

        # ── Process each scene ─────────────────────────────────────
        scene_videos    = []
        scene_durations = []
        audio_paths     = []

        for i, scene in enumerate(scenes):
            jobs[job_id]['progress'] = f"Processing scene {i+1}/{len(scenes)}"
            scene_path, duration, audio_path = create_scene_video(scene, temp_dir, i)
            scene_videos.append(scene_path)
            scene_durations.append(duration)
            audio_paths.append(audio_path)

        # ── Concatenate scenes ─────────────────────────────────────
        jobs[job_id]['progress'] = "Concatenating scenes..."
        raw_output = os.path.join(temp_dir, 'raw_video.mp4')
        concatenate_scenes(scene_videos, raw_output, temp_dir)

        # ── Transcribe each audio via OpenAI Whisper ───────────────
        jobs[job_id]['progress'] = "Transcribing audio for subtitles..."
        all_blocks = []
        cursor_sec = 0.0

        for i, audio_path in enumerate(audio_paths):
            print(f"[Whisper] Scene {i}...")
            blocks = transcribe_audio_to_srt_blocks(audio_path, start_offset_sec=cursor_sec)
            all_blocks.extend(blocks)
            cursor_sec += scene_durations[i]

        # ── Build & save SRT ───────────────────────────────────────
        srt_content = build_srt(all_blocks)
        srt_path = os.path.join(temp_dir, 'subtitles.srt')
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        print(f"SRT: {len(all_blocks)} blocks written")

        # ── Burn subtitles into final video ───────────────────────
        jobs[job_id]['progress'] = "Burning subtitles..."
        final_output = os.path.join(temp_dir, 'final_video.mp4')

        if all_blocks:
            burn_subtitles(raw_output, srt_path, final_output)
        else:
            print("No Whisper segments returned, skipping subtitle burn.")
            shutil.copy(raw_output, final_output)

        file_size = os.path.getsize(final_output)
        jobs[job_id].update({
            'status': 'done',
            'file_path': final_output,
            'temp_dir': temp_dir,
            'file_size_mb': round(file_size / 1024 / 1024, 2),
            'progress': 'Done'
        })
        print(f"Job {job_id} completed: {file_size} bytes")

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
        shutil.rmtree(temp_dir, ignore_errors=True)


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route('/generate-video', methods=['POST'])
def generate_video():
    try:
        scenes = request.json
        print(f"Received request. Type: {type(scenes)}, Length: {len(scenes) if isinstance(scenes, list) else 'N/A'}")

        if not scenes or not isinstance(scenes, list):
            return jsonify({"success": False, "error": "Invalid input"}), 400

        for i, scene in enumerate(scenes):
            print(f"  Input scene {i}: audioFileId={scene.get('audioFileId')}, sceneIndex={scene.get('sceneIndex')}")

        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "queued", "progress": "Starting...",
            "file_size_mb": None, "file_path": None,
            "temp_dir": None, "error": None
        }

        print(f"Starting job {job_id} with {len(scenes)} scenes")
        thread = threading.Thread(target=process_video_job, args=(job_id, scenes), daemon=True)
        thread.start()
        print(f"Thread started for job {job_id}")

        return jsonify({
            "success": True, "job_id": job_id,
            "status": "queued",
            "message": "Video generation started. Poll /status/<job_id> for updates."
        })

    except Exception as e:
        import traceback
        print(f"Error in generate_video: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/status/<job_id>', methods=['GET'])
def get_status(job_id):
    if job_id not in jobs:
        return jsonify({"success": False, "error": "Job not found"}), 404
    job = jobs[job_id]
    return jsonify({
        "success": True, "job_id": job_id,
        "status": job['status'], "progress": job.get('progress'),
        "file_size_mb": job.get('file_size_mb'),
        "error": job.get('error'),
        "download_url": f"/download/{job_id}" if job['status'] == 'done' else None
    })


@app.route('/download/<job_id>', methods=['GET'])
def download_video(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    job = jobs[job_id]
    if job['status'] != 'done':
        return jsonify({"error": f"Not ready: {job['status']}"}), 400
    file_path = job.get('file_path')
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, mimetype='video/mp4',
                     as_attachment=True, download_name='news_video.mp4')


@app.route('/echo', methods=['POST'])
def echo():
    data = request.json
    summary = []
    if isinstance(data, list):
        for i, scene in enumerate(data):
            summary.append({
                "index": i,
                "sceneIndex": scene.get('sceneIndex'),
                "audioFileId": scene.get('audioFileId'),
                "text_preview": scene.get('text', '')[:50]
            })
    return jsonify(summary)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "ffmpeg": shutil.which('ffmpeg') or "NOT FOUND",
        "ffprobe": shutil.which('ffprobe') or "NOT FOUND",
        "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
        "active_jobs": len(jobs)
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
