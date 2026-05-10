from flask import Flask, request, jsonify
import subprocess
import requests
import os
import tempfile
import threading
import uuid
import shutil

app = Flask(__name__)

# In-memory job store
jobs = {}


def download_file(url, dest_path):
    """Download file following redirects"""
    session = requests.Session()
    response = session.get(url, stream=True, allow_redirects=True)
    
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            url = url + f"&confirm={value}"
            response = session.get(url, stream=True, allow_redirects=True)
            break
    
    with open(dest_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)
    return dest_path


def get_audio_duration(audio_path):
    """Get exact audio duration using ffprobe on local file"""
    result = subprocess.run([
        'ffprobe',
        '-v', 'error',
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
    """Create MP4 for one scene"""
    
    # Download audio first
    audio_id = scene['audioFileId']
    audio_url = f"https://drive.google.com/uc?export=download&id={audio_id}"
    audio_path = os.path.join(temp_dir, f"audio_{scene_index}.mp3")
    print(f"Downloading audio for scene {scene_index}...")
    download_file(audio_url, audio_path)
    
    audio_size = os.path.getsize(audio_path)
    print(f"Audio size: {audio_size} bytes")
    if audio_size < 1000:
        raise Exception(f"Audio download failed or too small: {audio_size} bytes")
    
    # Get exact duration from local file
    duration = get_audio_duration(audio_path)
    print(f"Scene {scene_index} duration: {duration}s")
    
    num_images = len(scene['images'])
    time_per_image = duration / num_images
    
    # Download images
    image_paths = []
    for i, img_url in enumerate(scene['images']):
        img_path = os.path.join(temp_dir, f"img_{scene_index}_{i}.jpg")
        download_file(img_url, img_path)
        image_paths.append(img_path)
    
    # Build FFmpeg filter
    filter_parts = []
    inputs = []
    
    for i, img_path in enumerate(image_paths):
        inputs.extend(['-loop', '1', '-t', str(time_per_image), '-i', img_path])
        zoom = "zoom+0.001" if i % 2 == 0 else "if(lte(zoom,1.0),1.5,max(1.001,zoom-0.001))"
        filter_parts.append(
            f"[{i}]scale=1920:1080,setsar=1,"
            f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={int(time_per_image*25)}:s=1920x1080:fps=25[v{i}]"
        )
    
    concat_inputs = ''.join([f'[v{i}]' for i in range(num_images)])
    filter_parts.append(f"{concat_inputs}concat=n={num_images}:v=1:a=0[base]")
    
    audio_input_idx = num_images
    inputs.extend(['-i', audio_path])
    
    # Escape text for FFmpeg
    text = scene.get('text', '')
    text = text.replace('\\', '\\\\')
    text = text.replace("'", "\u2019")
    text = text.replace(':', '\\:')
    text = text.replace(',', '\\,')
    
    filter_parts.append(
        f"[base]drawtext="
        f"text='{text}':"
        f"fontsize=38:"
        f"fontcolor=white:"
        f"box=1:boxcolor=black@0.6:boxborderw=12:"
        f"x=(w-text_w)/2:"
        f"y=h-120[outv]"
    )
    
    filter_complex = ';'.join(filter_parts)
    scene_output = os.path.join(temp_dir, f"scene_{scene_index}.mp4")
    
    cmd = ['ffmpeg', '-y']
    cmd.extend(inputs)
    cmd.extend([
        '-filter_complex', filter_complex,
        '-map', '[outv]',
        '-map', f'{audio_input_idx}:a',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-shortest',
        '-r', '25',
        scene_output
    ])
    
    print(f"Running FFmpeg for scene {scene_index}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise Exception(f"FFmpeg failed (code {result.returncode}): {result.stderr[-800:]}")
    
    if not os.path.exists(scene_output) or os.path.getsize(scene_output) < 1000:
        raise Exception(f"FFmpeg produced no output. Stderr: {result.stderr[-500:]}")
    
    print(f"Scene {scene_index} done: {os.path.getsize(scene_output)} bytes")
    return scene_output


def concatenate_scenes(scene_videos, output_path, temp_dir):
    """Concatenate all scene MP4s into final video"""
    
    concat_file = os.path.join(temp_dir, 'concat.txt')
    with open(concat_file, 'w') as f:
        for video_path in scene_videos:
            f.write(f"file '{video_path}'\n")
    
    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat',
        '-safe', '0',
        '-i', concat_file,
        '-c', 'copy',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise Exception(f"Concat failed: {result.stderr[-500:]}")
    
    print(f"Final video size: {os.path.getsize(output_path)} bytes")
    return output_path


def process_video_job(job_id, scenes):
    """Background thread: generate video and store result"""
    
    # Create a persistent temp dir (not auto-deleted)
    temp_dir = tempfile.mkdtemp(prefix=f"job_{job_id}_")
    
    try:
        jobs[job_id]['status'] = 'processing'
        
        scene_videos = []
        for i, scene in enumerate(scenes):
            jobs[job_id]['progress'] = f"Processing scene {i+1}/{len(scenes)}"
            print(f"Job {job_id}: Processing scene {i+1}/{len(scenes)}")
            scene_path = create_scene_video(scene, temp_dir, i)
            scene_videos.append(scene_path)
        
        jobs[job_id]['progress'] = "Concatenating scenes..."
        final_output = os.path.join(temp_dir, 'final_video.mp4')
        concatenate_scenes(scene_videos, final_output, temp_dir)
        
        file_size = os.path.getsize(final_output)
        
        # Store final video path for download
        jobs[job_id]['status'] = 'done'
        jobs[job_id]['file_path'] = final_output
        jobs[job_id]['temp_dir'] = temp_dir
        jobs[job_id]['file_size_mb'] = round(file_size / 1024 / 1024, 2)
        jobs[job_id]['progress'] = "Done"
        print(f"Job {job_id} completed: {file_size} bytes")
        
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)
        # Cleanup on error
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route('/generate-video', methods=['POST'])
def generate_video():
    """Start async video generation, return job ID immediately"""
    try:
        scenes = request.json
        
        if not scenes or not isinstance(scenes, list):
            return jsonify({"success": False, "error": "Invalid input"}), 400
        
        # Create job
        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "queued",
            "progress": "Starting...",
            "file_size_mb": None,
            "error": None
        }
        
        # Start background thread
        thread = threading.Thread(
            target=process_video_job,
            args=(job_id, scenes),
            daemon=True
        )
        thread.start()
        
        # Return immediately with job ID
        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "queued",
            "message": "Video generation started. Poll /status/<job_id> for updates."
        })
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/status/<job_id>', methods=['GET'])
def get_status(job_id):
    """Poll this endpoint to check job progress"""
    
    if job_id not in jobs:
        return jsonify({"success": False, "error": "Job not found"}), 404
    
    job = jobs[job_id]
    
    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": job['status'],        # queued / processing / done / error
        "progress": job.get('progress'),
        "file_size_mb": job.get('file_size_mb'),
        "error": job.get('error'),
        "download_url": f"/download/{job_id}" if job['status'] == 'done' else None
    })


@app.route('/download/<job_id>', methods=['GET'])
def download_video(job_id):
    """Download the final video file"""
    from flask import send_file
    
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    
    job = jobs[job_id]
    
    if job['status'] != 'done':
        return jsonify({"error": f"Job not ready. Status: {job['status']}"}), 400
    
    file_path = job.get('file_path')
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Video file not found"}), 404
    
    return send_file(
        file_path,
        mimetype='video/mp4',
        as_attachment=True,
        download_name='news_video.mp4'
    )


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "ffmpeg": shutil.which('ffmpeg') or "NOT FOUND",
        "ffprobe": shutil.which('ffprobe') or "NOT FOUND",
        "active_jobs": len(jobs)
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)