#!/usr/bin/env python3
import json
import sys
import os

LH_GROUP_CODES = {"LH", "LX", "OS", "SN", "EW", "4Y", "EN"}


def is_month_key(k: str) -> bool:
    try:
        i = int(str(k))
        return 1 <= i <= 12
    except Exception:
        return False


def validate_catalog(path: str) -> int:
    errors = 0
    warnings = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"ERROR: Failed to read JSON from {path}: {exc}")
        return 2

    if not isinstance(data, list):
        print("ERROR: Catalog must be a JSON array of destination objects.")
        return 2

    for idx, rec in enumerate(data):
        ctx = f"entry[{idx}]"
        if not isinstance(rec, dict):
            print(f"ERROR: {ctx} is not an object")
            errors += 1
            continue
        code = rec.get("code")
        if not isinstance(code, str) or not code.isalpha() or len(code) != 3:
            print(f"ERROR: {ctx} missing/invalid 'code' (IATA 3 letters)")
            errors += 1
        else:
            if code.upper() != code:
                print(f"WARN: {ctx} code is not uppercase -> {code}")
                warnings += 1
        for field in ("city", "country"):
            if not rec.get(field) or not isinstance(rec.get(field), str):
                print(f"ERROR: {ctx} missing/invalid '{field}'")
                errors += 1
        tags = rec.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            print(f"ERROR: {ctx} 'tags' must be an array of strings")
            errors += 1
        # avgHighCByMonth: keys 1..12 (as strings)
        avg_map = rec.get("avgHighCByMonth") or {}
        if not isinstance(avg_map, dict):
            print(f"ERROR: {ctx} 'avgHighCByMonth' must be an object")
            errors += 1
        else:
            for k, v in avg_map.items():
                if not is_month_key(k):
                    print(f"ERROR: {ctx} avgHighCByMonth invalid key '{k}' (expect '1'..'12')")
                    errors += 1
                if not isinstance(v, (int, float)):
                    print(f"ERROR: {ctx} avgHighCByMonth['{k}'] must be number")
                    errors += 1
        water = rec.get("waterTempCByMonth")
        if water is not None:
            if not isinstance(water, dict):
                print(f"ERROR: {ctx} 'waterTempCByMonth' must be an object if present")
                errors += 1
            else:
                for k, v in water.items():
                    if not is_month_key(k):
                        print(f"ERROR: {ctx} waterTempCByMonth invalid key '{k}'")
                        errors += 1
                    if not isinstance(v, (int, float)):
                        print(f"ERROR: {ctx} waterTempCByMonth['{k}'] must be number")
                        errors += 1
        snow = rec.get("snowReliability")
        if snow is not None:
            if not isinstance(snow, dict):
                print(f"ERROR: {ctx} 'snowReliability' must be an object if present")
                errors += 1
            else:
                for k, v in snow.items():
                    if not is_month_key(k):
                        print(f"ERROR: {ctx} snowReliability invalid key '{k}'")
                        errors += 1
                    try:
                        fv = float(v)
                    except Exception:
                        print(f"ERROR: {ctx} snowReliability['{k}'] must be number in [0,1]")
                        errors += 1
                        continue
                    if fv < 0 or fv > 1:
                        print(f"WARN: {ctx} snowReliability['{k}'] outside [0,1]: {fv}")
                        warnings += 1
        carriers = rec.get("lhGroupCarriers")
        if carriers is not None:
            if not isinstance(carriers, list) or not all(isinstance(c, str) for c in carriers):
                print(f"ERROR: {ctx} 'lhGroupCarriers' must be an array of strings")
                errors += 1
            else:
                for c in carriers:
                    if c not in LH_GROUP_CODES:
                        print(f"WARN: {ctx} unknown LH group carrier '{c}'")
                        warnings += 1

    print(f"Checked {len(data)} entries. errors={errors} warnings={warnings}")
    return 1 if errors else 0


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "lh_destinations_catalog.json")
    return validate_catalog(path)


if __name__ == "__main__":
    raise SystemExit(main())

