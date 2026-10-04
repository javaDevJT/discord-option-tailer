#!/usr/bin/env bash
set -euo pipefail

# prepare runs before Buildx; scan runs in the composite action; publish runs
# only after the scan action succeeds. Credentials remain in Docker's config.
case "${1:-}" in
  prepare)
    : "${SECURITY_REPORT_DIRECTORY:?}" "${SECURITY_PUBLISH:?}" "${GITHUB_OUTPUT:?}"
    mkdir -p "$SECURITY_REPORT_DIRECTORY"
    archive="$SECURITY_REPORT_DIRECTORY.oci.tar"
    repository="container-security-local"
    : > "$SECURITY_REPORT_DIRECTORY/release-tags.txt"
    if [[ "$SECURITY_PUBLISH" == true ]]; then
      : "${SECURITY_RELEASE_TAGS:?}"
      repository=""
      while IFS= read -r tag; do
        [[ -z "$tag" ]] && continue
        [[ "$tag" =~ ^ghcr\.io/[a-zA-Z0-9._/-]+:[a-zA-Z0-9_.-]+$ ]] || {
          echo "Unsupported release tag: $tag" >&2; exit 2;
        }
        candidate="${tag%:*}"
        if [[ -n "$repository" && "$candidate" != "$repository" ]]; then
          echo "One build must publish to one image repository" >&2; exit 2
        fi
        repository="$candidate"
        printf '%s\n' "$tag" >> "$SECURITY_REPORT_DIRECTORY/release-tags.txt"
      done <<< "$SECURITY_RELEASE_TAGS"
      [[ -s "$SECURITY_REPORT_DIRECTORY/release-tags.txt" ]] || exit 2
      exporter='type=image,push-by-digest=true,name-canonical=true,push=true'
    elif [[ "$SECURITY_PUBLISH" == false ]]; then
      exporter="type=oci,dest=$archive"
    else
      echo "SECURITY_PUBLISH must be true or false" >&2; exit 2
    fi
    printf 'repository=%s\nexporter=%s\narchive=%s\n' "$repository" "$exporter" "$archive" >> "$GITHUB_OUTPUT"
    ;;
  scan)
    : "${SECURITY_IMAGE:?}" "${SECURITY_PLATFORMS:?}" "${SECURITY_REPORT_DIRECTORY:?}" "${SYFT_CMD:?}" "${GRYPE_CMD:?}"
    mkdir -p "$SECURITY_REPORT_DIRECTORY"
    printf '%s\n' "$SECURITY_IMAGE" > "$SECURITY_REPORT_DIRECTORY/scanned-image.txt"
    printf '%s\n' "$SECURITY_PLATFORMS" > "$SECURITY_REPORT_DIRECTORY/scanned-platforms.txt"
    : > "$SECURITY_REPORT_DIRECTORY/gate.txt"
    # Explicit empty configurations prevent project ignore rules from silently
    # excluding findings or turning this into an only-fixed scan.
    printf '{}\n' > "$SECURITY_REPORT_DIRECTORY/syft-config.yaml"
    printf '{}\n' > "$SECURITY_REPORT_DIRECTORY/grype-config.yaml"
    export SYFT_CHECK_FOR_APP_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false
    export GRYPE_DB_AUTO_UPDATE=true GRYPE_DB_VALIDATE_AGE=true
    failed=0
    count=0
    IFS=',' read -r -a platforms <<< "${SECURITY_PLATFORMS//$'\n'/,}"
    for platform in "${platforms[@]}"; do
      platform="${platform//[[:space:]]/}"
      [[ "$platform" =~ ^linux/(amd64|arm64)(/v[0-9]+)?$ ]] || {
        echo "Unsupported or empty image platform: $platform" >&2; exit 2;
      }
      count=$((count + 1))
      suffix="${platform//\//-}"
      sbom="$SECURITY_REPORT_DIRECTORY/$suffix.syft.json"
      report="$SECURITY_REPORT_DIRECTORY/$suffix.grype.json"
      echo "Generating SBOM and scanning $platform"
      if ! "$SYFT_CMD" scan "$SECURITY_IMAGE" --platform "$platform" \
        --config "$SECURITY_REPORT_DIRECTORY/syft-config.yaml" \
        --output "syft-json=$sbom"; then
        echo "Syft failed for $platform" >&2
        printf '%s FAIL syft\n' "$platform" >> "$SECURITY_REPORT_DIRECTORY/gate.txt"
        failed=1
        continue
      fi
      if "$GRYPE_CMD" "sbom:$sbom" --config "$SECURITY_REPORT_DIRECTORY/grype-config.yaml" \
        --fail-on high --output json --file "$report"; then
        printf '%s PASS\n' "$platform" >> "$SECURITY_REPORT_DIRECTORY/gate.txt"
      else
        status=$?
        printf '%s FAIL exit=%s\n' "$platform" "$status" >> "$SECURITY_REPORT_DIRECTORY/gate.txt"
        echo "Grype failed for $platform (exit $status)" >&2
        failed=1
      fi
    done
    [[ "$count" -gt 0 ]] || exit 2
    [[ "$failed" == 0 ]] || exit 1
    ;;
  publish)
    : "${SECURITY_IMAGE:?}" "${SECURITY_REPORT_DIRECTORY:?}"
    [[ "$SECURITY_IMAGE" =~ ^ghcr\.io/[a-zA-Z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || {
      echo "Publication requires an immutable GHCR digest" >&2; exit 2;
    }
    [[ -s "$SECURITY_REPORT_DIRECTORY/gate.txt" ]] || {
      echo "Missing successful security gate evidence" >&2; exit 2;
    }
    if grep -q ' FAIL' "$SECURITY_REPORT_DIRECTORY/gate.txt"; then
      echo "Failed security gate prevents release tagging" >&2; exit 2
    fi
    [[ "$(cat "$SECURITY_REPORT_DIRECTORY/scanned-image.txt")" == "registry:$SECURITY_IMAGE" ]] || {
      echo "Gate evidence belongs to a different image" >&2; exit 2;
    }
    args=(docker buildx imagetools create --prefer-index=false)
    while IFS= read -r tag; do
      [[ -z "$tag" ]] && continue
      args+=(--tag "$tag")
    done < "$SECURITY_REPORT_DIRECTORY/release-tags.txt"
    [[ "${#args[@]}" -gt 5 ]] || exit 2
    "${args[@]}" "$SECURITY_IMAGE"
    digest="${SECURITY_IMAGE##*@}"
    while IFS= read -r tag; do
      [[ -z "$tag" ]] && continue
      actual="$(docker buildx imagetools inspect "$tag" --format '{{.Manifest.Digest}}')"
      [[ "$actual" == "$digest" ]] || {
        echo "Published digest differs from scanned image for $tag" >&2; exit 3;
      }
      printf '%s %s\n' "$tag" "$actual" >> "$SECURITY_REPORT_DIRECTORY/published-digests.txt"
    done < "$SECURITY_REPORT_DIRECTORY/release-tags.txt"
    ;;
  *) echo "Usage: $0 prepare|scan|publish" >&2; exit 2 ;;
esac
