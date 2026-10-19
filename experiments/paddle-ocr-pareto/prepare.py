"""Rebuild public inputs, verify pinned bytes, and download the official models."""
import argparse
import os
import shutil
import subprocess

from common import CONFIG, ROOT, SETTINGS, WORK, read, save, sha


def inputs():
    for path, item in read(CONFIG / "documents.json").items():
        destination = ROOT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(".download")
            subprocess.run(["curl", "--fail", "--location", "--retry", "2",
                            "--connect-timeout", "20", "--max-time", "300",
                            item["url"], "--output", str(temporary)], check=True)
            if sha(temporary) != item["sha256"]:
                temporary.unlink()
                raise RuntimeError("Public PDF changed: " + path)
            temporary.replace(destination)
        if sha(destination) != item["sha256"]:
            raise RuntimeError("Input hash mismatch: " + path)
    models = WORK / "models" / "tessdata_fast"
    models.mkdir(parents=True, exist_ok=True)
    pinned = {
        "chi_sim": "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730",
        "chi_tra": "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b",
        "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
        "vie": "79df64caf7bcfb2a27df5042ecb6121e196eada34da774956995747636d5bfa1",
    }
    receipts = []
    for lang, expected in pinned.items():
        path = models / (lang + ".traineddata")
        url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{lang}.traineddata"
        local = __import__("pathlib").Path("/opt/homebrew/share/tessdata") / path.name
        if not path.exists():
            if local.is_file() and sha(local) == expected:
                shutil.copyfile(local, path)
            else:
                subprocess.run(["curl", "--fail", "--location", "--retry", "2", url,
                                "--output", str(path)], check=True)
        if sha(path) != expected:
            raise RuntimeError("Tesseract model hash mismatch: " + lang)
        receipts.append({"language": lang, "url": url, "sha256": expected})
    save(WORK / "tesseract-model-sources.json", receipts)


def paddle_model(tier):
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(WORK / "paddlex-cache")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    from paddleocr import PaddleOCR
    return PaddleOCR(
        text_detection_model_name=f"PP-OCRv6_{tier}_det",
        text_recognition_model_name=f"PP-OCRv6_{tier}_rec",
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False, device="cpu", cpu_threads=SETTINGS["cpu_threads"],
        enable_mkldnn=False, text_rec_score_thresh=SETTINGS["text_rec_score_thresh"],
        text_det_limit_side_len=SETTINGS["detection_limit_side_len"],
        text_det_limit_type=SETTINGS["detection_limit_type"],
        return_word_box=SETTINGS["paddle_word_boxes"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs-only", action="store_true")
    args = parser.parse_args()
    inputs()
    if not args.inputs_only:
        for tier in ("tiny", "small", "medium"):
            paddle_model(tier)
            print(tier, "downloaded and loaded", flush=True)
