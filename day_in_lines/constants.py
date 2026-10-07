"""Sizes, phases and file types shared by every module."""
import numpy as np

MAX_UPLOAD_BYTES = 40_000_000
MAX_UPLOAD_PIXELS = 60_000_000    # a 60-megapixel photo is fine; a decompression bomb is not
BAKE_VERSION = "v2"      # bump when planning or dithering changes, so stored frames are made again

W, H = 800, 480          # logical sheet (5:3, matches 7.5" 800x480 panels)
SS = 2                   # supersampling for the graphite render
TOTAL = 720              # drawing events per day
PHASES = (("composition", 40), ("linework", 230), ("shading", None), ("finishing", 50))
PAPER_RGB = np.array([0.965, 0.948, 0.905], np.float32)   # preview only
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
UPLOAD_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tif"}
