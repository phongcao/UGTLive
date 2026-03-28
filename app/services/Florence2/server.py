"""FastAPI server for Florence-2 OCR service."""

import sys
import os
import time
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from io import BytesIO
from typing import List, Dict

# Fix SSL certificate verification for urllib.request
# This is needed because bundled Python may not have access to system certificates
# Must be done BEFORE any imports that might trigger model downloads
import ssl
import certifi
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image
import torch

# Add shared folder to path
shared_dir = Path(__file__).parent.parent / "shared"
print(f"[DEBUG] Script location: {Path(__file__).absolute()}")
print(f"[DEBUG] Shared directory: {shared_dir.absolute()}")
print(f"[DEBUG] Shared directory exists: {shared_dir.exists()}")
if shared_dir.exists():
    print(f"[DEBUG] Files in shared: {list(shared_dir.glob('*.py'))}")
sys.path.insert(0, str(shared_dir))
print(f"[DEBUG] sys.path[0]: {sys.path[0]}")

from config_parser import parse_service_config, get_config_value
from response_models import OCRResponse, ErrorResponse, ServiceInfo, ShutdownResponse, TextObject, ColorInfo
from color_analysis import extract_foreground_background_colors, attach_color_info

# Load service configuration
config_path = Path(__file__).parent / "service_config.txt"
SERVICE_CONFIG = parse_service_config(str(config_path))

# Get service settings
SERVICE_NAME = get_config_value(SERVICE_CONFIG, 'service_name', 'Florence2')
SERVICE_PORT = int(get_config_value(SERVICE_CONFIG, 'port', '5006'))
SERVICE_INSTALL_VERSION = get_config_value(SERVICE_CONFIG, 'service_install_version', '1')

# Global model and processor
MODEL = None
PROCESSOR = None
DEVICE = None
TORCH_DTYPE = None


@asynccontextmanager
async def lifespan(app):
    """Pre-load Florence-2 model at startup and clean up on shutdown."""
    global MODEL, PROCESSOR

    print("=" * 60)
    print("PRE-LOADING FLORENCE-2 MODEL AT STARTUP")
    print("=" * 60)

    try:
        initialize_model()
        print("[OK] Florence-2 model pre-loaded successfully")
    except Exception as e:
        print(f"[FAIL] Failed to pre-load Florence-2 model: {e}")
        print("Model will be loaded on first request instead.")

    # Pre-load color extractor
    try:
        from color_analysis import _get_color_extractor
        _get_color_extractor()
        print("[OK] Color extractor pre-loaded successfully")
    except Exception as e:
        print(f"[FAIL] Failed to pre-load color extractor: {e}")

    print("[OK] All models ready - service is ready for requests!")
    print("=" * 60)

    yield

    # Cleanup on shutdown
    del MODEL, PROCESSOR
    torch.cuda.empty_cache()


# Initialize FastAPI app
app = FastAPI(title=SERVICE_NAME, version=SERVICE_INSTALL_VERSION, lifespan=lifespan)


def initialize_model():
    """Initialize the Florence-2 model and processor."""
    global MODEL, PROCESSOR, DEVICE, TORCH_DTYPE

    if MODEL is not None:
        print("Using existing Florence-2 model")
        return

    # Determine device and dtype
    if torch.cuda.is_available():
        DEVICE = "cuda:0"
        TORCH_DTYPE = torch.float16
        device_name = torch.cuda.get_device_name(0)
        print(f"GPU is available: {device_name}. Using GPU for OCR.")
    else:
        DEVICE = "cpu"
        TORCH_DTYPE = torch.float32
        print("GPU is not available. Florence-2 will use CPU.")

    print("Loading Florence-2-large model...")
    start_time = time.time()

    from transformers import AutoProcessor, AutoModelForCausalLM

    model_name = "microsoft/Florence-2-large"
    MODEL = AutoModelForCausalLM.from_pretrained(
        model_name,
        torch_dtype=TORCH_DTYPE,
        trust_remote_code=True,
        attn_implementation="eager",
    ).to(DEVICE)
    PROCESSOR = AutoProcessor.from_pretrained(
        model_name,
        trust_remote_code=True
    )

    initialization_time = time.time() - start_time
    print(f"Florence-2 initialization completed in {initialization_time:.2f} seconds")


def run_florence_task(image: Image.Image, task_prompt: str, text_input: str = None):
    """Run a Florence-2 task on an image."""
    if text_input is not None:
        prompt = task_prompt + text_input
    else:
        prompt = task_prompt

    inputs = PROCESSOR(text=prompt, images=image, return_tensors="pt").to(DEVICE, TORCH_DTYPE)

    generated_ids = MODEL.generate(
        input_ids=inputs["input_ids"],
        pixel_values=inputs["pixel_values"],
        max_new_tokens=4096,
        num_beams=3,
        do_sample=False
    )

    generated_text = PROCESSOR.batch_decode(generated_ids, skip_special_tokens=False)[0]

    parsed_answer = PROCESSOR.post_process_generation(
        generated_text,
        task=task_prompt,
        image_size=(image.width, image.height)
    )

    return parsed_answer


def process_ocr_results(image: Image.Image, results: dict) -> List[Dict]:
    """Process Florence-2 OCR_WITH_REGION results into standardized format.
    
    Florence-2 OCR_WITH_REGION returns:
    {
        '<OCR_WITH_REGION>': {
            'quad_boxes': [[x1,y1,x2,y2,x3,y3,x4,y4], ...],
            'labels': ['text1', 'text2', ...]
        }
    }
    """
    text_objects = []

    ocr_data = results.get('<OCR_WITH_REGION>', {})
    quad_boxes = ocr_data.get('quad_boxes', [])
    labels = ocr_data.get('labels', [])

    for i, (quad, text) in enumerate(zip(quad_boxes, labels)):
        if not text or not text.strip():
            continue

        # Florence-2 may prepend special tokens like </s> to labels
        text = text.replace('</s>', '').strip()
        if not text:
            continue

        # quad is [x1,y1, x2,y2, x3,y3, x4,y4] (4 corners)
        # Convert to vertices format: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
        vertices = [
            [int(quad[0]), int(quad[1])],
            [int(quad[2]), int(quad[3])],
            [int(quad[4]), int(quad[5])],
            [int(quad[6]), int(quad[7])]
        ]

        # Calculate bounding box from vertices
        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]

        x = min(xs)
        y = min(ys)
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)

        # Extract color information
        color_info = None
        try:
            bbox = vertices
            color_data = extract_foreground_background_colors(image, bbox)
            if color_data:
                color_info = color_data
        except Exception as e:
            print(f"Color extraction failed: {e}")

        # Build text object
        text_obj = {
            "text": text.strip(),
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "vertices": vertices,
            "confidence": None,
            "text_orientation": "horizontal"
        }

        # Attach color information if available
        if color_info:
            attach_color_info(text_obj, color_info)

        text_objects.append(text_obj)

    return text_objects


@app.post("/process")
async def process_image(request: Request):
    """
    Process an image for OCR using Florence-2.

    Expects binary image data in the request body.
    Query parameters:
    - lang: Language code (default: 'japan') - Note: Florence-2 is language-agnostic for OCR
    """
    try:
        start_time = time.time()

        # Get query parameters
        lang = request.query_params.get('lang', 'japan')

        # Read binary image data
        image_bytes = await request.body()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="No image data provided")

        # Load image from binary data
        image = Image.open(BytesIO(image_bytes)).convert('RGB')

        # Initialize model if needed
        initialize_model()

        # Run OCR with region detection
        results = run_florence_task(image, '<OCR_WITH_REGION>')

        # Process results
        text_objects = process_ocr_results(image, results)

        # Calculate processing time
        processing_time = time.time() - start_time

        # Determine backend
        backend = "gpu" if torch.cuda.is_available() else "cpu"

        # Build response
        response = {
            "status": "success",
            "texts": text_objects,
            "processing_time": processing_time,
            "language": lang,
            "char_level": False,
            "backend": backend
        }

        return JSONResponse(content=response)

    except Exception as e:
        print(f"Error processing image: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e),
                "error_type": type(e).__name__
            }
        )


@app.post("/analyze_color")
async def analyze_color(request: Request):
    """
    Analyze image for foreground/background colors.

    Expects binary image data in the request body.
    """
    try:
        # Read binary image data
        image_bytes = await request.body()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="No image data provided")

        # Load image from binary data
        image = Image.open(BytesIO(image_bytes)).convert('RGB')

        # Use the whole image as the region
        width, height = image.size
        bbox = [[0, 0], [width, 0], [width, height], [0, height]]

        # Extract colors
        color_info = extract_foreground_background_colors(image, bbox)

        if not color_info:
            return JSONResponse(content={
                "status": "success",
                "color_info": None
            })

        return JSONResponse(content={
            "status": "success",
            "color_info": color_info
        })

    except Exception as e:
        print(f"Error analyzing color: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e),
                "error_type": type(e).__name__
            }
        )


@app.get("/info")
async def get_info():
    """Get service information."""
    info = {
        "service_name": get_config_value(SERVICE_CONFIG, 'service_name', 'Florence2'),
        "description": get_config_value(SERVICE_CONFIG, 'description', ''),
        "service_install_version": get_config_value(SERVICE_CONFIG, 'service_install_version', '1'),
        "venv_name": get_config_value(SERVICE_CONFIG, 'venv_name', 'ugt_florence2'),
        "port": int(get_config_value(SERVICE_CONFIG, 'port', '5006')),
        "server_url": get_config_value(SERVICE_CONFIG, 'server_url', 'http://127.0.0.1'),
        "local_only": get_config_value(SERVICE_CONFIG, 'local_only', 'true') == 'true',
        "github_url": get_config_value(SERVICE_CONFIG, 'github_url', ''),
        "service_author": get_config_value(SERVICE_CONFIG, 'service_author', '')
    }
    return JSONResponse(content=info)


@app.post("/shutdown")
async def shutdown():
    """Shutdown the service."""
    print("Shutdown request received...")

    async def shutdown_task():
        await asyncio.sleep(1)
        os._exit(0)

    asyncio.create_task(shutdown_task())

    return JSONResponse(content={
        "status": "success",
        "message": "Service shutting down"
    })





if __name__ == "__main__":
    host = "127.0.0.1" if get_config_value(SERVICE_CONFIG, 'local_only', 'true') == 'true' else "0.0.0.0"

    print(f"Starting {SERVICE_NAME} service on {host}:{SERVICE_PORT}")
    print(f"Configuration: {SERVICE_CONFIG}")

    uvicorn.run(
        app,
        host=host,
        port=SERVICE_PORT,
        log_level="info",
        access_log=True
    )
