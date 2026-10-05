#!/usr/bin/env bash
set -euo pipefail

die() {
  echo "[container-security] $*" >&2
  exit 2
}

trim() {
  local value=$1
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  printf '%s' "$value"
}

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as image:
    for chunk in iter(lambda: image.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

# Read the Buildx OCI archive without extracting untrusted paths. The returned
# reference is the descriptor annotation verbatim, so callers do not guess how
# BuildKit encoded the exporter name or tag.
archive_descriptor() {
  python3 - "$1" <<'PY'
import hashlib
import json
import re
import sys
import tarfile

path = sys.argv[1]

def fail(message):
    print(f"Invalid OCI archive: {message}", file=sys.stderr)
    raise SystemExit(1)

def read_member(archive, name):
    try:
        member = archive.getmember(name)
    except KeyError:
        fail(f"missing {name}")
    if not member.isfile():
        fail(f"{name} is not a regular file")
    stream = archive.extractfile(member)
    if stream is None:
        fail(f"cannot read {name}")
    return stream.read()

def read_json(archive, name):
    try:
        return json.loads(read_member(archive, name))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail(f"invalid JSON in {name}")

def read_blob(archive, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        fail("unsupported or malformed blob digest")
    data = read_member(archive, f"blobs/sha256/{digest[7:]}")
    if hashlib.sha256(data).hexdigest() != digest[7:]:
        fail(f"blob digest mismatch for {digest}")
    return data

def platform_name(descriptor):
    platform = descriptor.get("platform") or {}
    os_name = platform.get("os")
    architecture = platform.get("architecture")
    if os_name == "unknown" and architecture == "unknown":
        annotations = descriptor.get("annotations") or {}
        if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
            return None
    if not os_name or not architecture:
        fail("image manifest has no platform metadata")
    result = f"{os_name}/{architecture}"
    variant = platform.get("variant")
    if variant:
        result += f"/{variant}"
    if not re.fullmatch(r"linux/(?:amd64|arm64)(?:/v[0-9]+)?", result):
        fail(f"unsupported image platform {result}")
    return result

try:
    with tarfile.open(path, "r:*") as archive:
        layout = read_json(archive, "oci-layout")
        if layout.get("imageLayoutVersion") != "1.0.0":
            fail("unsupported OCI layout version")
        index = read_json(archive, "index.json")
        descriptors = index.get("manifests")
        if index.get("schemaVersion") != 2 or not isinstance(descriptors, list) or len(descriptors) != 1:
            fail("expected exactly one named image in the OCI layout")
        descriptor = descriptors[0]
        annotations = descriptor.get("annotations") or {}
        reference = annotations.get("org.opencontainers.image.ref.name")
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+,-]*", reference) or "," in reference:
            fail("missing or ambiguous OCI image reference annotation")
        digest = descriptor.get("digest")
        document = read_json(archive, f"blobs/sha256/{digest[7:]}") if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else None
        if document is None:
            fail("missing or malformed image manifest digest")
        raw_document = read_blob(archive, digest)
        if json.loads(raw_document) != document:
            fail("inconsistent image manifest")

        media_type = descriptor.get("mediaType", "")
        platforms = []
        if "image.index" in media_type or "manifest.list" in media_type or isinstance(document.get("manifests"), list):
            child_descriptors = document.get("manifests")
            if not isinstance(child_descriptors, list):
                fail("image index has no manifest list")
            for child in child_descriptors:
                name = platform_name(child)
                if name is not None:
                    platforms.append(name)
        else:
            platform = descriptor.get("platform")
            if not platform:
                config = document.get("config") or {}
                try:
                    config_document = json.loads(read_blob(archive, config.get("digest")))
                except json.JSONDecodeError:
                    fail("invalid image config JSON")
                platform = {
                    "os": config_document.get("os"),
                    "architecture": config_document.get("architecture"),
                    "variant": config_document.get("variant"),
                }
            name = platform_name({"platform": platform})
            if name is not None:
                platforms.append(name)
        if not platforms:
            fail("image has no scannable platforms")
        if len(set(platforms)) != len(platforms):
            fail("image has duplicate platform manifests")
        print(f"{reference}\t{digest}\t{','.join(platforms)}")
except (OSError, tarfile.TarError, KeyError) as error:
    fail(str(error))
PY
}

parsed_platforms=()
validate_platforms() {
  local raw piece platform prior
  local -a pieces=()
  raw=$(trim "${1:-}")
  [[ -n "$raw" ]] || die "SECURITY_PLATFORMS is empty"
  [[ "$raw" != ,* && "$raw" != *, && "$raw" != *,,* ]] || die "SECURITY_PLATFORMS has an empty entry"
  IFS=',' read -r -a pieces <<< "$raw"
  parsed_platforms=()
  for piece in "${pieces[@]}"; do
    platform=$(trim "$piece")
    [[ "$platform" =~ ^linux/(amd64|arm64)(/v[0-9]+)?$ ]] || die "Unsupported or empty image platform: $platform"
    for prior in "${parsed_platforms[@]:-}"; do
      [[ "$prior" != "$platform" ]] || die "Duplicate image platform: $platform"
    done
    parsed_platforms+=("$platform")
  done
  [[ ${#parsed_platforms[@]} -gt 0 ]] || die "SECURITY_PLATFORMS is empty"
}

validate_archive_platforms() {
  local actual_list=$1 platform expected found
  validate_platforms "$actual_list"
  local actual_platforms=("${parsed_platforms[@]}")
  [[ ${#actual_platforms[@]} -eq ${#requested_platforms[@]} ]] || die "OCI archive platform set differs from requested platforms"
  for platform in "${actual_platforms[@]}"; do
    found=false
    for expected in "${requested_platforms[@]}"; do
      [[ "$expected" == "$platform" ]] && found=true
    done
    [[ "$found" == true ]] || die "OCI archive contains an unrequested platform: $platform"
  done
  for platform in "${requested_platforms[@]}"; do
    found=false
    for expected in "${actual_platforms[@]}"; do
      [[ "$expected" == "$platform" ]] && found=true
    done
    [[ "$found" == true ]] || die "OCI archive is missing requested platform: $platform"
  done
}

read_one_line() {
  local file=$1 first extra
  [[ -s "$file" ]] || die "Missing evidence file: $file"
  exec 3< "$file"
  IFS= read -r first <&3 || [[ -n "$first" ]] || { exec 3<&-; die "Invalid evidence file: $file"; }
  [[ -n "$first" ]] || { exec 3<&-; die "Invalid evidence file: $file"; }
  if IFS= read -r extra <&3 || [[ -n "$extra" ]]; then
    exec 3<&-
    die "Expected one line in evidence file: $file"
  fi
  exec 3<&-
  REPLY=$first
}

validate_gate() {
  local line platform prior expected found
  local -a seen=()
  [[ -s "$SECURITY_REPORT_DIRECTORY/gate.txt" ]] || die "Missing successful security gate evidence"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "PASS "* ]] || die "Invalid or failed security gate row"
    platform=${line#PASS }
    [[ "$platform" =~ ^linux/(amd64|arm64)(/v[0-9]+)?$ ]] || die "Invalid platform in security gate"
    for prior in "${seen[@]:-}"; do
      [[ "$prior" != "$platform" ]] || die "Duplicate security gate result for $platform"
    done
    seen+=("$platform")
  done < "$SECURITY_REPORT_DIRECTORY/gate.txt"
  [[ ${#seen[@]} -eq ${#requested_platforms[@]} ]] || die "Security gate coverage is incomplete or contains extra platforms"
  for expected in "${requested_platforms[@]}"; do
    found=false
    for platform in "${seen[@]}"; do
      [[ "$expected" == "$platform" ]] && found=true
    done
    [[ "$found" == true ]] || die "Security gate is missing PASS for $expected"
  done
}

case "${1:-}" in
  prepare)
    : "${SECURITY_REPORT_DIRECTORY:?}" "${SECURITY_PUBLISH:?}" "${GITHUB_OUTPUT:?}"
    mkdir -p "$SECURITY_REPORT_DIRECTORY"
    archive="$SECURITY_REPORT_DIRECTORY.oci.tar"
    repository="container-security-local"
    tags_file="$SECURITY_REPORT_DIRECTORY/release-tags.txt"
    : > "$tags_file"
    if [[ "$SECURITY_PUBLISH" == true ]]; then
      : "${SECURITY_RELEASE_TAGS:?}"
      repository=""
      declare -a seen_tags=()
      while IFS= read -r tag || [[ -n "$tag" ]]; do
        [[ -n "$tag" ]] || die "Release tags contain an empty entry"
        [[ "$tag" =~ ^ghcr\.io/[a-zA-Z0-9._/-]+:[a-zA-Z0-9_.-]+$ ]] || die "Unsupported release tag: $tag"
        duplicate=false
        for prior in "${seen_tags[@]:-}"; do
          [[ "$prior" != "$tag" ]] || duplicate=true
        done
        [[ "$duplicate" == false ]] || die "Duplicate release tag: $tag"
        seen_tags+=("$tag")
        candidate="${tag%:*}"
        if [[ -n "$repository" && "$candidate" != "$repository" ]]; then
          die "One build must publish to one image repository"
        fi
        repository=$candidate
        printf '%s\n' "$tag" >> "$tags_file"
      done <<< "$SECURITY_RELEASE_TAGS"
      [[ -s "$tags_file" ]] || die "No release tags were provided"
    elif [[ "$SECURITY_PUBLISH" != false ]]; then
      die "SECURITY_PUBLISH must be true or false"
    fi
    exporter="type=oci,dest=$archive,name=security-scan"
    printf '%s\n' "$repository" > "$SECURITY_REPORT_DIRECTORY/repository.txt"
    sha256_file "$tags_file" > "$SECURITY_REPORT_DIRECTORY/release-tags.sha256"
    printf 'repository=%s\nexporter=%s\narchive=%s\n' "$repository" "$exporter" "$archive" >> "$GITHUB_OUTPUT"
    ;;

  scan)
    : "${SECURITY_ARCHIVE:?}" "${SECURITY_PLATFORMS:?}" "${SECURITY_REPORT_DIRECTORY:?}" "${SYFT_CMD:?}" "${GRYPE_CMD:?}"
    archive=$SECURITY_ARCHIVE
    [[ -f "$archive" && ! -L "$archive" ]] || die "OCI archive is missing or is not a regular file"
    mkdir -p "$SECURITY_REPORT_DIRECTORY"
    validate_platforms "$SECURITY_PLATFORMS"
    requested_platforms=("${parsed_platforms[@]}")
    gate_tmp="$SECURITY_REPORT_DIRECTORY/gate.txt.tmp"
    gate_file="$SECURITY_REPORT_DIRECTORY/gate.txt"
    rm -f "$gate_file" "$gate_tmp" \
      "$SECURITY_REPORT_DIRECTORY/scanned-archive.sha256" \
      "$SECURITY_REPORT_DIRECTORY/scanned-image-digest.txt" \
      "$SECURITY_REPORT_DIRECTORY/scanned-oci-reference.txt" \
      "$SECURITY_REPORT_DIRECTORY/requested-platforms.txt"
    scan_archive="${archive%.tar}.scan.$$.oci.tar"
    trap 'rm -f "$scan_archive" "$gate_tmp"' EXIT
    initial_sha=$(sha256_file "$archive")
    [[ "$initial_sha" =~ ^[a-f0-9]{64}$ ]] || die "Unable to hash OCI archive"
    cp -- "$archive" "$scan_archive"
    chmod 400 "$scan_archive"
    [[ "$(sha256_file "$scan_archive")" == "$initial_sha" && "$(sha256_file "$archive")" == "$initial_sha" ]] || die "OCI archive changed while creating scan snapshot"
    metadata=$(archive_descriptor "$scan_archive") || die "Unable to read OCI archive descriptor"
    IFS=$'\t' read -r oci_reference image_digest archive_platforms <<< "$metadata"
    [[ -n "$oci_reference" && "$image_digest" =~ ^sha256:[a-f0-9]{64}$ ]] || die "Invalid OCI archive descriptor"
    validate_archive_platforms "$archive_platforms"

    # Empty policies prevent repository-local configuration from suppressing
    # findings or turning this into an only-fixed scan.
    printf '{}\n' > "$SECURITY_REPORT_DIRECTORY/syft-config.yaml"
    printf '{}\n' > "$SECURITY_REPORT_DIRECTORY/grype-config.yaml"
    export SYFT_CHECK_FOR_APP_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false
    export GRYPE_DB_AUTO_UPDATE=true GRYPE_DB_VALIDATE_AGE=true
    # Syft resolves local OCI archives by filename; appending an OCI ref makes
    # the input stop resolving as a file. The archive contains one image, so
    # Syft can select it directly. ORAS still uses the exact parsed ref below.
    image_source="oci-archive:$scan_archive"
    for platform in "${requested_platforms[@]}"; do
      suffix="${platform//\//-}"
      sbom="$SECURITY_REPORT_DIRECTORY/$suffix.syft.json"
      report="$SECURITY_REPORT_DIRECTORY/$suffix.grype.json"
      echo "Generating SBOM and scanning $platform"
      if ! "$SYFT_CMD" scan "$image_source" --platform "$platform" \
        --config "$SECURITY_REPORT_DIRECTORY/syft-config.yaml" \
        --output "syft-json=$sbom"; then
        printf 'FAIL %s\n' "$platform" >> "$gate_tmp"
        mv "$gate_tmp" "$gate_file"
        die "Syft failed for $platform"
      fi
      if ! "$GRYPE_CMD" "sbom:$sbom" --config "$SECURITY_REPORT_DIRECTORY/grype-config.yaml" \
        --fail-on high --output json --file "$report"; then
        printf 'FAIL %s\n' "$platform" >> "$gate_tmp"
        mv "$gate_tmp" "$gate_file"
        die "Grype failed for $platform"
      fi
      printf 'PASS %s\n' "$platform" >> "$gate_tmp"
    done
    [[ "$(sha256_file "$scan_archive")" == "$initial_sha" && "$(sha256_file "$archive")" == "$initial_sha" ]] || {
      printf 'FAIL archive-integrity\n' >> "$gate_tmp"
      mv "$gate_tmp" "$gate_file"
      die "OCI archive changed during security scanning"
    }
    printf '%s\n' "${requested_platforms[@]}" > "$SECURITY_REPORT_DIRECTORY/requested-platforms.txt"
    printf '%s\n' "$initial_sha" > "$SECURITY_REPORT_DIRECTORY/scanned-archive.sha256"
    printf '%s\n' "$image_digest" > "$SECURITY_REPORT_DIRECTORY/scanned-image-digest.txt"
    printf '%s\n' "$oci_reference" > "$SECURITY_REPORT_DIRECTORY/scanned-oci-reference.txt"
    mv "$gate_tmp" "$gate_file"
    ;;

  publish)
    : "${SECURITY_ARCHIVE:?}" "${SECURITY_PLATFORMS:?}" "${SECURITY_REPORT_DIRECTORY:?}" "${ORAS_CMD:?}"
    archive=$SECURITY_ARCHIVE
    [[ -f "$archive" && ! -L "$archive" ]] || die "OCI archive is missing or is not a regular file"
    validate_platforms "$SECURITY_PLATFORMS"
    requested_platforms=("${parsed_platforms[@]}")
    validate_gate

    requested_file="$SECURITY_REPORT_DIRECTORY/requested-platforms.txt"
    [[ -s "$requested_file" ]] || die "Missing requested-platform evidence"
    stored_platforms=()
    while IFS= read -r platform || [[ -n "$platform" ]]; do
      [[ "$platform" =~ ^linux/(amd64|arm64)(/v[0-9]+)?$ ]] || die "Invalid requested-platform evidence"
      stored_platforms+=("$platform")
    done < "$requested_file"
    [[ ${#stored_platforms[@]} -eq ${#requested_platforms[@]} ]] || die "Gate evidence belongs to a different platform request"
    for index in "${!requested_platforms[@]}"; do
      [[ "${stored_platforms[$index]}" == "${requested_platforms[$index]}" ]] || die "Gate evidence belongs to a different platform request"
    done
    read_one_line "$SECURITY_REPORT_DIRECTORY/scanned-archive.sha256"
    scanned_archive_sha=$REPLY
    [[ "$scanned_archive_sha" =~ ^[a-f0-9]{64}$ ]] || die "Invalid scanned archive digest"
    read_one_line "$SECURITY_REPORT_DIRECTORY/scanned-image-digest.txt"
    scanned_image_digest=$REPLY
    [[ "$scanned_image_digest" =~ ^sha256:[a-f0-9]{64}$ ]] || die "Invalid scanned image digest"
    read_one_line "$SECURITY_REPORT_DIRECTORY/scanned-oci-reference.txt"
    scanned_oci_reference=$REPLY
    [[ "$(sha256_file "$archive")" == "$scanned_archive_sha" ]] || die "OCI archive changed after security scanning"

    tags_file="$SECURITY_REPORT_DIRECTORY/release-tags.txt"
    read_one_line "$SECURITY_REPORT_DIRECTORY/release-tags.sha256"
    tags_sha=$REPLY
    [[ "$tags_sha" =~ ^[a-f0-9]{64}$ && "$(sha256_file "$tags_file")" == "$tags_sha" ]] || die "Release tag evidence changed after preparation"
    read_one_line "$SECURITY_REPORT_DIRECTORY/repository.txt"
    prepared_repository=$REPLY
    release_tags=()
    repository=""
    while IFS= read -r tag || [[ -n "$tag" ]]; do
      [[ "$tag" =~ ^ghcr\.io/[a-zA-Z0-9._/-]+:[a-zA-Z0-9_.-]+$ ]] || die "Invalid prepared release tag"
      candidate="${tag%:*}"
      if [[ -n "$repository" && "$candidate" != "$repository" ]]; then
        die "Prepared release tags span multiple repositories"
      fi
      for prior in "${release_tags[@]:-}"; do
        [[ "$prior" != "$tag" ]] || die "Duplicate prepared release tag"
      done
      repository=$candidate
      release_tags+=("$tag")
    done < "$tags_file"
    [[ ${#release_tags[@]} -gt 0 && "$repository" == "$prepared_repository" ]] || die "No valid prepared release tags"
    command -v "$ORAS_CMD" >/dev/null 2>&1 || die "ORAS command is unavailable"

    umask 077
    publish_archive="${archive%.tar}.publish.$$.oci.tar"
    trap 'rm -f "$publish_archive"' EXIT
    cp -- "$archive" "$publish_archive"
    chmod 400 "$publish_archive"
    [[ "$(sha256_file "$publish_archive")" == "$scanned_archive_sha" && "$(sha256_file "$archive")" == "$scanned_archive_sha" ]] || die "OCI archive changed while creating publication snapshot"
    metadata=$(archive_descriptor "$publish_archive") || die "Unable to read OCI archive descriptor"
    IFS=$'\t' read -r oci_reference image_digest archive_platforms <<< "$metadata"
    [[ "$oci_reference" == "$scanned_oci_reference" && "$image_digest" == "$scanned_image_digest" ]] || die "OCI archive descriptor differs from scanned evidence"
    validate_archive_platforms "$archive_platforms"
    : > "$SECURITY_REPORT_DIRECTORY/published-digests.txt"
    for tag in "${release_tags[@]}"; do
      [[ "$(sha256_file "$publish_archive")" == "$scanned_archive_sha" && "$(sha256_file "$archive")" == "$scanned_archive_sha" ]] || die "OCI archive changed before publication"
      # Suppress registry-client diagnostics because implementations may echo
      # auth metadata on failures. The local action reports only the safe target.
      if ! "$ORAS_CMD" cp --from-oci-layout "$publish_archive:$oci_reference" "$tag" >/dev/null 2>&1; then
        die "ORAS failed to publish $tag"
      fi
      if ! remote_descriptor=$("$ORAS_CMD" manifest fetch --descriptor "$tag" 2>/dev/null); then
        die "Unable to verify published digest for $tag"
      fi
      if ! remote_digest=$(printf '%s' "$remote_descriptor" | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])' 2>/dev/null); then
        die "Unable to parse published digest for $tag"
      fi
      [[ "$remote_digest" == "$scanned_image_digest" ]] || die "Published image digest differs from scanned OCI manifest for $tag"
      printf '%s %s\n' "$tag" "$remote_digest" >> "$SECURITY_REPORT_DIRECTORY/published-digests.txt"
    done
    [[ "$(sha256_file "$publish_archive")" == "$scanned_archive_sha" && "$(sha256_file "$archive")" == "$scanned_archive_sha" ]] || die "OCI archive changed during publication"
    ;;

  *) echo "Usage: $0 prepare|scan|publish" >&2; exit 2 ;;
esac
