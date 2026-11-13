"""OCR backends.

Windows: Microsoft's OneOCR (copied at runtime from the Snipping Tool that ships with Windows 11;
it is not redistributed with RemoteSub), or the bundled open-source RapidOCR when it is missing.
macOS: Apple's Vision framework, built into the system.
"""
import io
import os
import shutil
import subprocess
import sys

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


def describe(app_dir):
    """Which engine OCR will use, for the setup window."""
    if sys.platform == "darwin":
        return "Apple Vision"
    if prepare_oneocr(os.path.join(app_dir, "oneocr")):
        return "OneOCR (Windows Ekran Alıntısı Aracı)"
    return "RapidOCR (Ekran Alıntısı Aracı yok)"


class OcrEngine:
    def __init__(self, app_dir, log=print):
        self.name = None
        if sys.platform == "darwin":
            self._init_vision()
            return
        oneocr_dir = os.path.join(app_dir, "oneocr")
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

    def _init_vision(self):
        import Vision
        from Foundation import NSData
        self._vision, self._nsdata = Vision, NSData
        self.name = "Apple Vision"

    def lines(self, image):
        """Text lines in a PIL image, top to bottom."""
        if self.name == "OneOCR":
            return [line.get("text", "") for line in self._oneocr.recognize_pil(image).get("lines", [])]
        if self.name == "Apple Vision":
            return self._vision_lines(image)
        result = self._rapid(self._np.asarray(image.convert("RGB")))
        return list(result.txts or [])

    def _vision_lines(self, image):
        buf = io.BytesIO()
        image.save(buf, "PNG")
        data = self._nsdata.dataWithBytes_length_(buf.getvalue(), buf.tell())
        handler = self._vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        request = self._vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(self._vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["en-US"])
        request.setUsesLanguageCorrection_(True)
        handler.performRequests_error_([request], None)
        found = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if candidates:
                # Vision's origin is bottom-left, so a higher y is further up the image.
                found.append((-observation.boundingBox().origin.y, candidates[0].string()))
        return [text for _, text in sorted(found)]
