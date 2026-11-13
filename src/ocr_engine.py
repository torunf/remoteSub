"""OCR backends: Windows' OneOCR (taken from the Snipping Tool) when available, RapidOCR otherwise.

OneOCR is Microsoft's and is not redistributed with RemoteSub; it is copied at runtime from the
Snipping Tool that ships with Windows 11. RapidOCR (open source) is bundled as the fallback.
"""
import os
import shutil
import subprocess

ONEOCR_FILES = ("oneocr.dll", "oneocr.onemodel", "onnxruntime.dll")


def _snipping_tool_dirs():
    try:
        location = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-AppxPackage -Name Microsoft.ScreenSketch).InstallLocation"],
            capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
    except Exception:
        location = ""
    dirs = []
    if location:
        dirs += [os.path.join(location, "SnippingTool"), location]
    dirs.append(os.path.join(os.path.expanduser("~"), ".config", "oneocr"))
    return dirs


def prepare_oneocr(target_dir):
    """Make sure the OneOCR files are in target_dir; returns False when Windows has none to copy."""
    if all(os.path.exists(os.path.join(target_dir, f)) for f in ONEOCR_FILES):
        return True
    for source in _snipping_tool_dirs():
        if all(os.path.exists(os.path.join(source, f)) for f in ONEOCR_FILES):
            os.makedirs(target_dir, exist_ok=True)
            for f in ONEOCR_FILES:
                shutil.copy2(os.path.join(source, f), target_dir)
            return True
    return False


class OcrEngine:
    def __init__(self, oneocr_dir, log=print):
        self.name = None
        if prepare_oneocr(oneocr_dir):
            try:
                import oneocr
                oneocr.CONFIG_DIR = oneocr_dir
                self._oneocr = oneocr.OcrEngine()
                if getattr(self._oneocr, "ocr_dll", None):
                    self.name = "OneOCR"
                    return
            except Exception as e:
                log(f"OneOCR unavailable, using RapidOCR: {e}")
        # Imported only here: RapidOCR's onnxruntime must not be loaded next to OneOCR's.
        import numpy
        from rapidocr import RapidOCR
        self._np = numpy
        self._rapid = RapidOCR()
        self.name = "RapidOCR"

    def lines(self, image):
        """Text lines in a PIL image, top to bottom."""
        if self.name == "OneOCR":
            return [line.get("text", "") for line in self._oneocr.recognize_pil(image).get("lines", [])]
        result = self._rapid(self._np.asarray(image.convert("RGB")))
        return list(result.txts or [])
