#!/usr/bin/env python3
"""Write a compact High/Critical-only copy of supported JSON scan reports."""

import argparse
import glob
import json
import math
import os
from pathlib import Path
import sys
import tempfile


class ReportError(Exception):
    """Raised when an input is malformed or has an unsupported report shape."""


def _fail(message):
    raise ReportError(message)


def _object(value, where):
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _array(value, where):
    if not isinstance(value, list):
        _fail(f"{where} must be an array")
    return value


def _required_text(obj, key, where):
    if key not in obj or not isinstance(obj[key], str) or not obj[key].strip():
        _fail(f"{where}.{key} must be a non-empty string")
    return obj[key].strip()


def _optional_text(obj, key, where):
    if key not in obj or obj[key] is None:
        return None
    if not isinstance(obj[key], str):
        _fail(f"{where}.{key} must be a string or null")
    value = obj[key].strip()
    return value or None


def _severity(value, accepted, where):
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be a recognized severity string")
    normalized = value.strip().upper()
    if normalized not in accepted:
        _fail(f"{where} has an unsupported severity")
    if normalized == "CRITICAL":
        return "CRITICAL"
    if normalized == "HIGH":
        return "HIGH"
    return None


def _string_list(value, where):
    if value is None:
        return []
    values = _array(value, where)
    result = []
    for index, item in enumerate(values):
        if not isinstance(item, str):
            _fail(f"{where}[{index}] must be a string")
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return sorted(result)


def _optional_line(obj, key, where):
    if key not in obj or obj[key] is None:
        return None
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{where}.{key} must be a positive integer or null")
    return value


def _finding(tool, report_name, severity, finding_id, package=None,
             installed_version=None, fixed_versions=None, locations=None):
    item = {
        "tool": tool,
        "report": report_name,
        "severity": severity,
        "id": finding_id,
    }
    if package:
        item["package"] = package
    if installed_version:
        item["installedVersion"] = installed_version
    if fixed_versions:
        item["fixedVersions"] = fixed_versions
    if locations:
        item["locations"] = locations
    return item


def _grype(document, report_name):
    matches = _array(document["matches"], "matches")
    findings = []
    accepted = {"NEGLIGIBLE", "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN", "INFO"}
    for index, raw_match in enumerate(matches):
        where = f"matches[{index}]"
        match = _object(raw_match, where)
        vulnerability = _object(match.get("vulnerability"), f"{where}.vulnerability")
        severity = _severity(vulnerability.get("severity"), accepted,
                             f"{where}.vulnerability.severity")
        finding_id = _required_text(vulnerability, "id", f"{where}.vulnerability")
        artifact = _object(match.get("artifact"), f"{where}.artifact")
        package = _optional_text(artifact, "name", f"{where}.artifact")
        installed_version = _optional_text(artifact, "version", f"{where}.artifact")

        fix = vulnerability.get("fix")
        if fix is None:
            fixed_versions = []
        else:
            fix = _object(fix, f"{where}.vulnerability.fix")
            fixed_versions = _string_list(fix.get("versions", []),
                                          f"{where}.vulnerability.fix.versions")

        raw_locations = artifact.get("locations", [])
        locations = []
        for location_index, raw_location in enumerate(_array(raw_locations, f"{where}.artifact.locations")):
            location = _object(raw_location, f"{where}.artifact.locations[{location_index}]")
            path = _optional_text(location, "path", f"{where}.artifact.locations[{location_index}]")
            if path:
                locations.append({"path": path})

        if severity:
            findings.append(_finding("grype", report_name, severity, finding_id,
                                     package, installed_version, fixed_versions,
                                     _unique_locations(locations)))
    return findings


def _trivy(document, report_name):
    results = _array(document["Results"], "Results")
    findings = []
    accepted = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
    for result_index, raw_result in enumerate(results):
        where = f"Results[{result_index}]"
        result = _object(raw_result, where)
        target = _optional_text(result, "Target", where)
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            continue
        for vulnerability_index, raw_vulnerability in enumerate(
                _array(vulnerabilities, f"{where}.Vulnerabilities")):
            vulnerability_where = f"{where}.Vulnerabilities[{vulnerability_index}]"
            vulnerability = _object(raw_vulnerability, vulnerability_where)
            severity = _severity(vulnerability.get("Severity"), accepted,
                                 f"{vulnerability_where}.Severity")
            finding_id = _required_text(vulnerability, "VulnerabilityID", vulnerability_where)
            package = _optional_text(vulnerability, "PkgName", vulnerability_where)
            installed_version = _optional_text(vulnerability, "InstalledVersion", vulnerability_where)
            fixed_version = _optional_text(vulnerability, "FixedVersion", vulnerability_where)
            fixed_versions = [fixed_version] if fixed_version else []
            package_path = _optional_text(vulnerability, "PkgPath", vulnerability_where)
            location = {}
            if target:
                location["target"] = target
            if package_path:
                location["path"] = package_path
            if severity:
                findings.append(_finding("trivy", report_name, severity, finding_id,
                                         package, installed_version, fixed_versions,
                                         [location] if location else []))
    return findings


def _security_score(properties, where):
    if properties is None:
        return None
    properties = _object(properties, where)
    if "security-severity" not in properties:
        return None
    value = properties["security-severity"]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail(f"{where}.security-severity must be a numeric score")
    try:
        score = float(value)
    except (TypeError, ValueError):
        _fail(f"{where}.security-severity must be a numeric score")
    if not math.isfinite(score) or score < 0 or score > 10:
        _fail(f"{where}.security-severity must be between 0 and 10")
    return score


def _sarif_locations(result, where):
    raw_locations = result.get("locations", [])
    output = []
    for index, raw_location in enumerate(_array(raw_locations, f"{where}.locations")):
        location_where = f"{where}.locations[{index}]"
        location = _object(raw_location, location_where)
        physical = location.get("physicalLocation")
        if physical is None:
            continue
        physical = _object(physical, f"{location_where}.physicalLocation")
        compact = {}
        artifact = physical.get("artifactLocation")
        if artifact is not None:
            artifact = _object(artifact, f"{location_where}.physicalLocation.artifactLocation")
            uri = _optional_text(artifact, "uri", f"{location_where}.physicalLocation.artifactLocation")
            if uri:
                # Query and fragment values are not useful source identity and may carry tokens.
                uri = uri.split("?", 1)[0].split("#", 1)[0]
                if uri:
                    compact["path"] = uri
        region = physical.get("region")
        if region is not None:
            region = _object(region, f"{location_where}.physicalLocation.region")
            start_line = _optional_line(region, "startLine", f"{location_where}.physicalLocation.region")
            end_line = _optional_line(region, "endLine", f"{location_where}.physicalLocation.region")
            if start_line is not None:
                compact["startLine"] = start_line
            if end_line is not None:
                if start_line is not None and end_line < start_line:
                    _fail(f"{location_where} has endLine before startLine")
                compact["endLine"] = end_line
        if compact:
            output.append(compact)
    return _unique_locations(output)


def _sarif(document, report_name):
    if document.get("version") != "2.1.0":
        _fail("SARIF version must be 2.1.0")
    runs = _array(document.get("runs"), "runs")
    findings = []
    for run_index, raw_run in enumerate(runs):
        run_where = f"runs[{run_index}]"
        run = _object(raw_run, run_where)
        tool = _object(run.get("tool"), f"{run_where}.tool")
        driver = _object(tool.get("driver"), f"{run_where}.tool.driver")
        _required_text(driver, "name", f"{run_where}.tool.driver")
        rules = _array(driver.get("rules", []), f"{run_where}.tool.driver.rules")
        rules_by_id = {}
        for rule_index, raw_rule in enumerate(rules):
            rule_where = f"{run_where}.tool.driver.rules[{rule_index}]"
            rule = _object(raw_rule, rule_where)
            rule_id = _optional_text(rule, "id", rule_where)
            if rule_id:
                if rule_id in rules_by_id:
                    _fail(f"{rule_where}.id duplicates a rule id")
                rules_by_id[rule_id] = rule

        results = run.get("results", [])
        if results is None:
            _fail(f"{run_where}.results must be an array")
        for result_index, raw_result in enumerate(_array(results, f"{run_where}.results")):
            result_where = f"{run_where}.results[{result_index}]"
            result = _object(raw_result, result_where)
            result_rule_id = _optional_text(result, "ruleId", result_where)
            rule_index = result.get("ruleIndex")
            descriptor = None
            if rule_index is not None:
                if isinstance(rule_index, bool) or not isinstance(rule_index, int) or not 0 <= rule_index < len(rules):
                    _fail(f"{result_where}.ruleIndex does not identify a rule")
                descriptor = rules[rule_index]
            elif result_rule_id:
                descriptor = rules_by_id.get(result_rule_id)
            descriptor_id = _optional_text(descriptor, "id", f"{result_where}.rule") if descriptor else None
            if result_rule_id and descriptor_id and result_rule_id != descriptor_id:
                _fail(f"{result_where}.ruleId disagrees with its ruleIndex")

            result_score = _security_score(result.get("properties"), f"{result_where}.properties")
            rule_score = _security_score(descriptor.get("properties"), f"{result_where}.rule.properties") if descriptor else None
            if result_score is not None and rule_score is not None and result_score != rule_score:
                _fail(f"{result_where} has conflicting security-severity scores")
            score = result_score if result_score is not None else rule_score
            if score is None or score < 7.0:
                continue

            finding_id = result_rule_id or descriptor_id
            if not finding_id:
                _fail(f"{result_where} has a security-severity score but no rule id")
            severity = "CRITICAL" if score >= 9.0 else "HIGH"
            findings.append(_finding("sarif", report_name, severity, finding_id,
                                     locations=_sarif_locations(result, result_where)))
    return findings


def _gitleaks(document, report_name):
    findings = []
    for index, raw_leak in enumerate(_array(document, "Gitleaks report")):
        where = f"Gitleaks report[{index}]"
        leak = _object(raw_leak, where)
        rule_id = _required_text(leak, "RuleID", where)
        file_path = _required_text(leak, "File", where)
        start_line = _optional_line(leak, "StartLine", where)
        end_line = _optional_line(leak, "EndLine", where)
        if start_line is not None and end_line is not None and end_line < start_line:
            _fail(f"{where} has EndLine before StartLine")
        location = {"path": file_path}
        if start_line is not None:
            location["startLine"] = start_line
        if end_line is not None:
            location["endLine"] = end_line
        # Gitleaks reports matches; their presence is retained as CRITICAL metadata.
        # Match, Secret, and all other raw leak fields are deliberately discarded.
        findings.append(_finding("gitleaks", report_name, "CRITICAL", rule_id,
                                 locations=[location]))
    return findings


def _unique_locations(locations):
    unique = {}
    for location in locations:
        key = json.dumps(location, sort_keys=True, separators=(",", ":"))
        unique[key] = location
    return [unique[key] for key in sorted(unique)]


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value):
    _fail(f"non-standard JSON constant {value}")


def _parse_report(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle, object_pairs_hook=_unique_json_object,
                                 parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ReportError) as exc:
        raise ReportError(f"{path.name}: cannot read valid JSON ({exc})") from exc

    report_name = path.name
    if isinstance(document, list):
        tool = "gitleaks"
        findings = _gitleaks(document, report_name)
    elif isinstance(document, dict):
        markers = []
        if "runs" in document or "version" in document:
            markers.append("sarif")
        if "matches" in document:
            markers.append("grype")
        if "Results" in document:
            markers.append("trivy")
        if len(markers) != 1:
            _fail(f"{report_name}: unsupported or ambiguous JSON report")
        tool = markers[0]
        if tool == "sarif":
            findings = _sarif(document, report_name)
        elif tool == "grype":
            findings = _grype(document, report_name)
        else:
            findings = _trivy(document, report_name)
    else:
        _fail(f"{report_name}: unsupported JSON root type")
    return tool, findings


def _matched_inputs(patterns, output_path):
    matches = set()
    for pattern in patterns:
        found = glob.glob(pattern)
        if not found:
            _fail(f"input pattern matched no files: {pattern}")
        for candidate in found:
            path = Path(candidate)
            if not path.is_file():
                _fail(f"input is not a regular file: {path.name}")
            resolved = path.resolve(strict=True)
            if resolved == output_path.resolve(strict=False):
                _fail("output path must not overwrite an input report")
            matches.add(resolved)
    return sorted(matches, key=lambda path: os.path.normcase(str(path)))


def _write_atomic(output_path, document):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_path.parent,
                                         prefix=f".{output_path.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, sort_keys=True,
                      separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _sort_finding(item):
    severity_order = 0 if item["severity"] == "CRITICAL" else 1
    locations = json.dumps(item.get("locations", []), sort_keys=True, separators=(",", ":"))
    return (item["report"], item["tool"], severity_order, item["id"],
            item.get("package", ""), item.get("installedVersion", ""), locations)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True,
                        metavar="FILE_OR_GLOB", help="scan report input; repeat to aggregate")
    parser.add_argument("--output", required=True, metavar="FILE",
                        help="compact JSON output path")
    parser.add_argument("--repository", help="optional repository identity")
    parser.add_argument("--revision", help="optional source revision")
    parser.add_argument("--image", help="optional scanned image identity")
    parser.add_argument("--platform", help="optional scanned image platform")
    args = parser.parse_args(argv)

    output_path = Path(args.output).expanduser().resolve(strict=False)
    try:
        inputs = _matched_inputs(args.input, output_path)
        reports = []
        findings = []
        for path in inputs:
            tool, report_findings = _parse_report(path)
            reports.append({
                "name": path.name,
                "tool": tool,
                "highCriticalCount": len(report_findings),
            })
            findings.extend(report_findings)
        document = {"schemaVersion": 1, "reports": reports,
                    "findings": sorted(findings, key=_sort_finding)}
        if args.repository is not None:
            document["repository"] = args.repository
        if args.revision is not None:
            document["revision"] = args.revision
        if args.image is not None:
            document["image"] = args.image
        if args.platform is not None:
            document["platform"] = args.platform
        _write_atomic(output_path, document)
    except (OSError, ReportError) as exc:
        print(f"minimize-security-reports: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
