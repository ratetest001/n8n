from flask import Flask, request, jsonify, send_file
import subprocess
import requests
import os
import tempfile
import threading
import uuid
import shutil

app = Flask(__name__)
jobs = {}


def download_file(url, dest_path):
    session = requests.Session()
    headers = {'User-Agent': 'Mozilla/5.0'}
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


def create_scene_video(scene, temp_dir, scene_index):
    audio_id = scene['audioFileId']
    audio_url = f"https://drive.usercontent.google.com/download?id={audio_id}&export=download&confirm=t"
    audio_path = os.path.join(temp_dir, f"audio_{scene_index}.mp3")
    print(f"[Scene {scene_index}] Downloading audio: {audio_id}")
    download_file(audio_url, audio_path)

    audio_size = os.path.getsize(audio_path)
    print(f"[Scene {scene_index}] Audio size: {audio_size} bytes")
    if audio_size < 1000:
        raise Exception(f"Audio too small: {audio_size} bytes")

    duration = get_audio_duration(audio_path)
    print(f"[Scene {scene_index}] Duration: {duration}s")
    if duration < 0.5:
        raise Exception(f"Duration too short: {duration}s")

    images = scene.get('images', [])
    num_images = len(images)
    time_per_image = round(duration / num_images, 3)

    image_paths = []
    for i, img_url in enumerate(images):
        img_path = os.path.join(temp_dir, f"img_{scene_index}_{i}.jpg")
        download_file(img_url, img_path)
        image_paths.append(img_path)

    clip_paths = []
    for i, img_path in enumerate(image_paths):
        clip_path = os.path.join(temp_dir, f"clip_{scene_index}_{i}.mp4")
        cmd = [
            'ffmpeg', '-y',
            '-loop', '1', '-i', img_path,
            '-t', str(time_per_image),
            '-vf', 'scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage',
            '-crf', '28', '-pix_fmt', 'yuv420p', '-r', '24', '-an',
            clip_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Clip {i} failed: {result.stderr[-300:]}")
        clip_paths.append(clip_path)

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

    scene_output = os.path.join(temp_dir, f"scene_{scene_index}.mp4")
    cmd = [
        'ffmpeg', '-y',
        '-i', silent_video,
        '-i', audio_path,
        '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
        '-shortest',
        scene_output
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Audio merge failed: {result.stderr[-400:]}")

    scene_size = os.path.getsize(scene_output)
    print(f"[Scene {scene_index}] Done: {scene_size} bytes")
    if scene_size < 1000:
        raise Exception(f"Scene empty: {scene_size} bytes")

    for cp in clip_paths:
        if os.path.exists(cp): os.remove(cp)
    if os.path.exists(silent_video): os.remove(silent_video)
    if os.path.exists(concat_file): os.remove(concat_file)

    return scene_output


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


def process_video_job(job_id, scenes):
    temp_dir = tempfile.mkdtemp(prefix=f"job_{job_id}_")
    try:
        jobs[job_id]['status'] = 'processing'
        print(f"Job {job_id}: Total scenes = {len(scenes)}")

        # Log all scene audioFileIds upfront
        for i, scene in enumerate(scenes):
            print(f"  Scene {i}: audioFileId={scene.get('audioFileId')} sceneIndex={scene.get('sceneIndex')}")

        scene_videos = []
        for i, scene in enumerate(scenes):
            jobs[job_id]['progress'] = f"Processing scene {i+1}/{len(scenes)}"
            scene_path = create_scene_video(scene, temp_dir, i)
            scene_videos.append(scene_path)

        jobs[job_id]['progress'] = "Concatenating..."
        final_output = os.path.join(temp_dir, 'final_video.mp4')
        concatenate_scenes(scene_videos, final_output, temp_dir)

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


@app.route('/generate-video', methods=['POST'])
def generate_video():
    try:
        scenes = request.json
        if not scenes or not isinstance(scenes, list):
            return jsonify({"success": False, "error": "Invalid input"}), 400

        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "queued", "progress": "Starting...",
            "file_size_mb": None, "file_path": None,
            "temp_dir": None, "error": None
        }

        thread = threading.Thread(
            target=process_video_job,
            args=(job_id, scenes), daemon=True
        )
        thread.start()

        return jsonify({
            "success": True, "job_id": job_id,
            "status": "queued",
            "message": "Video generation started. Poll /status/<job_id> for updates."
        })

    except Exception as e:
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
        "active_jobs": len(jobs)
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)