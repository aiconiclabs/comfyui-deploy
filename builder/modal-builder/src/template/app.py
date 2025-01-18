import os
import subprocess
import json
from modal import Image, App, asgi_app, Mount

# First, install required packages
try:
    from pydantic import BaseModel
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
except ImportError:
    subprocess.check_call(["pip", "install", "fastapi", "pydantic", "uvicorn", "python-multipart"])
    from pydantic import BaseModel
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware

from config import config

current_directory = os.path.dirname(os.path.realpath(__file__))
deploy_test = config["deploy_test"] == "True"

print(config)
print("deploy_test ", deploy_test)

# Create the Modal app
app = App(name=config["name"])

# Constants
COMFY_HOST = "127.0.0.1:8188"
COMFY_API_AVAILABLE_INTERVAL_MS = 50
COMFY_API_AVAILABLE_MAX_RETRIES = 500
COMFY_POLLING_INTERVAL_MS = 250
COMFY_POLLING_MAX_RETRIES = 1000

# Base image with common dependencies
base_image = (
    Image.debian_slim()
    .apt_install("git")
    .pip_install(
        "fastapi>=0.68.0",
        "pydantic>=2.0.0",
        "uvicorn>=0.15.0",
        "python-multipart",
        "httpx",
        "tqdm"
    )
    .pip_install("git+https://github.com/modal-labs/asgiproxy.git")
)

# Define target image based on deploy_test flag
if deploy_test:
    target_image = base_image
else:
    target_image = (
        base_image
        .env({
            "CIVITAI_TOKEN": config["civitai_token"],
        })
        .apt_install("wget", "libgl1-mesa-glx", "libglib2.0-0")
        .run_commands(
            # Basic comfyui setup
            "git clone https://github.com/comfyanonymous/ComfyUI.git /comfyui",
            "cd /comfyui && pip install xformers!=0.0.18 -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cu121",
            # Install comfyui manager
            "cd /comfyui/custom_nodes && git clone https://github.com/ltdrdata/ComfyUI-Manager.git",
            "cd /comfyui/custom_nodes/ComfyUI-Manager && git reset --hard 9c86f62b912f4625fe2b929c7fc61deb9d16f6d3",
            "cd /comfyui/custom_nodes/ComfyUI-Manager && pip install -r requirements.txt",
            "cd /comfyui/custom_nodes/ComfyUI-Manager && mkdir startup-scripts",
        )
        .copy_local_file(f"{current_directory}/data/start.sh", "/start.sh")
        .run_commands("chmod +x /start.sh")
        # Restore the custom nodes first
        .copy_local_file(f"{current_directory}/data/restore_snapshot.py", "/")
        .copy_local_file(f"{current_directory}/data/snapshot.json", "/comfyui/custom_nodes/ComfyUI-Manager/startup-scripts/restore-snapshot.json")
        .run_commands("python restore_snapshot.py")
        # Then install the models
        .copy_local_file(f"{current_directory}/data/install_deps.py", "/")
        .copy_local_file(f"{current_directory}/data/models.json", "/")
        .copy_local_file(f"{current_directory}/data/deps.json", "/")
        .run_commands("python install_deps.py")
    )

# Create FastAPI app
web_app = FastAPI()
web_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Input(BaseModel):
    prompt_id: str
    workflow_api: dict
    status_endpoint: str
    file_upload_endpoint: str

class RequestInput(BaseModel):
    input: Input

def spawn_comfyui_in_background():
    import socket
    process = subprocess.Popen(
        ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"],
        cwd="/comfyui"
    )
    while True:
        try:
            socket.create_connection(("127.0.0.1", 8188), timeout=1).close()
            print("ComfyUI webserver ready!")
            break
        except (socket.timeout, ConnectionRefusedError):
            retcode = process.poll()
            if retcode is not None:
                raise RuntimeError(f"ComfyUI main.py exited unexpectedly with code {retcode}")
    return process

@app.function(
    image=target_image,
    gpu=config["gpu"],
    allow_concurrent_inputs=100,
    concurrency_limit=1,
    timeout=10 * 60,
)
@asgi_app()
def comfyui_app():
    """Main ComfyUI application endpoint"""
    from asgiproxy.config import BaseURLProxyConfigMixin, ProxyConfig
    from asgiproxy.context import ProxyContext
    from asgiproxy.simple_proxy import make_simple_proxy_app

    process = spawn_comfyui_in_background()
    print(f"App URL: https://{config['name']}--comfyui-app.modal.run")

    proxy_config = type(
        "Config",
        (BaseURLProxyConfigMixin, ProxyConfig),
        {
            "upstream_base_url": f"http://127.0.0.1:8188",
            "rewrite_host_header": "127.0.0.1:8188",
        },
    )()

    return make_simple_proxy_app(ProxyContext(proxy_config))

# Export the app
if __name__ == "__main__":
    app.serve()