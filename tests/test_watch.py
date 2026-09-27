import json
from pathlib import Path
import importlib.util

SPEC = importlib.util.spec_from_file_location("watch", Path(__file__).resolve().parents[1] / "watch.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)

CFG = {
    "exclude_keywords": ["16G", "16GB", "二手", "拆机", "矿卡"],
    "thresholds": {"normal": 2999, "good_brand": 3099},
    "good_brands": ["微星", "MSI", "华硕", "ASUS", "技嘉", "GIGABYTE"],
}


def test_target_match():
    assert watch.is_target_gpu("微星 RTX 5060 Ti 8G 万图师", CFG)
    assert watch.is_target_gpu("GIGABYTE RTX5060Ti 8GB", CFG)
    assert not watch.is_target_gpu("RTX 5060 8G", CFG)
    assert not watch.is_target_gpu("RTX 5060 Ti 16G", CFG)
    assert not watch.is_target_gpu("二手 RTX 5060 Ti 8G", CFG)


def test_price_parse():
    assert watch.parse_price("券后 ¥2,879") == 2879
    assert watch.parse_price("到手价3099元") == 3099


def test_threshold():
    c1 = watch.Candidate("x", "京东", "微星 RTX 5060 Ti 8G", 3099, "https://x")
    c2 = watch.Candidate("x", "京东", "映众 RTX 5060 Ti 8G", 2999, "https://y")
    assert watch.threshold_for(c1, CFG) == 3099
    assert watch.threshold_for(c2, CFG) == 2999
