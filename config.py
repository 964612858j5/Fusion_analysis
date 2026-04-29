"""
block01/config.py — User-editable configuration constants.
"""

OME_TIFF_FILE = (
    "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
    "2025.12.20_Final_17209_16_Slice4_Scan1.ome.tif"
)
OUTPUT_DIR = (
    "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/"
    "pipeline_v2"
)

CHANNEL_NAME_MAP    = {}   # {"OME raw name": "display name"}
OVERVIEW_DOWNSAMPLE = 32
PREVIEW_DOWNSAMPLE  = 4
NORM_LOW            = 1.0
NORM_HIGH           = 99.5

INITIAL_GROUPS = {
    "epithelial": {
        "CK19": 1.5, "Gp3": 1.0, "HsBAg": 1.0,
    },
    "immune": {
        "CD3D": 1.0, "CD4": 0.8, "CD8": 0.8,
        "CD68": 1.0, "CD163": 0.8, "CD11b": 0.5,
        "CD11c": 0.5, "CD14": 0.5, "CD22": 0.5,
        "CCR7": 0.5, "TIM3": 0.5, "CD45RA": 0.5,
        "CD45RO": 0.5, "HLA-DR": 0.5,
    },
    "endothelial": {"CD31": 1.5},
}
NUCLEUS_CONFIG = {"channel": "DAPI", "weight": 1.0}

PHASE1_DIAMETERS = [20, 30, 40, 50, 70]
PHASE2_FLOW      = [0.2, 0.4, 0.6]
PHASE2_CELLPROB  = [-1.0, 0.0, 0.5, 1.0]
# Cellpose 4.0.1+: model_type argument is ignored; only cpsam is used.
DEFAULT_MODEL    = "cpsam"  # kept for JSON output only

PATCH_COLORS = ["#ff4444", "#44ff88", "#4488ff", "#ffdd44"]
ROI_COLORS   = [
    "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#cc5de8", "#20c997", "#f06595",
]
# Default ROI name list (extended on demand)
DEFAULT_ROI_NAMES = ["ROI_1", "ROI_2", "ROI_3", "ROI_4",
                     "ROI_5", "ROI_6", "ROI_7", "ROI_8"]

BG_CORR_MAX_TILE      = 4096
TOPHAT_RADIUS_DEFAULT = 35
CUCIM_SIGMA_DEFAULT   = 50
TOPHAT_RADIUS_RANGE   = (10, 150)
CUCIM_SIGMA_RANGE     = (20, 200)
