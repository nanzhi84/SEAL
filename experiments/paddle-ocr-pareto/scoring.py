"""Geometry-only region selection and global character edit distances."""
import csv
import io
import unicodedata


def normalize(value, language):
    value = unicodedata.normalize("NFC", value)
    return "".join(value.split()) if language in {"chi_sim", "chi_tra"} else " ".join(value.split())


def distance(reference, actual):
    row = list(range(len(actual) + 1))
    for i, char in enumerate(reference, 1):
        new = [i]
        for j, other in enumerate(actual, 1):
            new.append(min(row[j] + 1, new[-1] + 1, row[j - 1] + (char != other)))
        row = new
    return row[-1]


def tesseract_words(tsv):
    words = []
    for item in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if item["level"] != "5":
            continue
        x, y, w, h = (int(item[k]) for k in ("left", "top", "width", "height"))
        words.append({"text": item["text"], "bbox": [x, y, x + w, y + h]})
    return words


def region_scores(words, page, width, height, language):
    scores = []
    for region in page["regions"]:
        left, top, right, bottom = region["bbox_normalized"]
        chosen = [i for i, word in enumerate(words)
                  if left <= (word["bbox"][0] + word["bbox"][2]) / 2 / width <= right
                  and top <= (word["bbox"][1] + word["bbox"][3]) / 2 / height <= bottom]
        actual = normalize("".join(words[i].get("separator", " ") + words[i]["text"]
                                   for i in chosen), language)
        gold = normalize(region["reference"], language)
        scores.append({"id": region["id"], "bbox_normalized": region["bbox_normalized"],
                       "gold": gold, "actual": actual, "errors": distance(gold, actual),
                       "reference_characters": len(gold), "selected_word_indices": chosen})
    return scores


def frontier(configurations):
    for row in configurations:
        row["pareto"] = row["eligible"] and not any(
            other["eligible"] and other["language"] == row["language"]
            and other["cer"] <= row["cer"] and other["total_seconds"] <= row["total_seconds"]
            and (other["cer"] < row["cer"] or other["total_seconds"] < row["total_seconds"])
            for other in configurations)
    return configurations
