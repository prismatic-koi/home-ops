#!/usr/bin/env bash
#
# render.sh — produce a real `flate` render from a linked git worktree.
#
# `flate` cannot open a linked worktree, where `.git` is a pointer file: it
# treats the source as remote, fails on the absent deploy key, blocks about
# 62 kustomizations, and never exits (see #3659, #3700). This script builds a
# throwaway ordinary clone of the repository's `.bare` object store, points
# its origin at the exact URL the cluster's GitRepository uses, copies the
# caller's working-tree content (including uncommitted changes) into it, and
# renders there.
#
# Usage: scripts/render.sh
# Prints the path to the rendered manifest file on stdout. Nothing else goes
# to stdout. Everything else — progress, errors — goes to stderr.
set -euo pipefail

# The exact URL the cluster's GitRepository uses (kubernetes/cluster0/flux/
# config/cluster.yaml). A trailing `.git` on the ssh:// form hangs flate
# (#3659). Do not add it.
readonly ORIGIN_URL="ssh://git@github.com/prismatic-koi/home-ops"
readonly RENDER_TIMEOUT="${RENDER_TIMEOUT:-120}"

err() {
	echo "render.sh: error: $*" >&2
}

if ! command -v flate >/dev/null 2>&1; then
	err "flate is not on PATH. See AGENTS.md, \"Rendering flate output locally\"."
	exit 1
fi

if ! command -v git >/dev/null 2>&1; then
	err "git is not on PATH."
	exit 1
fi

git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || {
	err "not inside a git repository."
	exit 1
}
git_common_dir="$(cd "$(dirname "$git_common_dir")" && pwd)/$(basename "$git_common_dir")"

toplevel="$(git rev-parse --show-toplevel 2>/dev/null)" || {
	err "could not resolve the working tree root."
	exit 1
}

tmp_dir="$(mktemp -d)"
render_file="${tmp_dir}/rendered-manifests.yaml"
clone_dir="${tmp_dir}/clone"

cleanup() {
	rm -rf "${tmp_dir}"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# 1. Clone the bare object store into an ordinary (non-worktree) clone.
if ! git clone --quiet "${git_common_dir}" "${clone_dir}" >/dev/null 2>&1; then
	err "failed to clone ${git_common_dir}."
	exit 1
fi

# 2. Point origin at the exact URL the cluster's GitRepository uses.
git -C "${clone_dir}" remote set-url origin "${ORIGIN_URL}"

# 3. Copy the caller's working-tree content, including uncommitted changes,
#    over the clone. `tar` extraction only adds and overwrites — it never
#    deletes a file that is absent from the source — so a file the caller
#    deleted or renamed would otherwise survive from the clone's default-
#    branch content and appear in the render as extra or duplicated objects.
#    Clear the clone's tracked content first (keeping .git) so the copy
#    below produces an exact copy of the caller's tree, not a union with it.
find "${clone_dir}" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
tar --exclude=.git -C "${toplevel}" -cf - . | tar -C "${clone_dir}" -xf -

# 4. Render, under a timeout. Do not set FLATE_ALLOW_WORKTREE: this renders
#    from a normal clone, so the worktree guard must not fire. If it does,
#    something above is wrong.
if (cd "${clone_dir}" && timeout "${RENDER_TIMEOUT}" flate build all \
	--path kubernetes/cluster0/flux) \
	>"${render_file}" 2>"${tmp_dir}/flate.stderr"; then
	:
else
	status=$?
	if [ "${status}" -eq 124 ]; then
		err "flate render timed out after ${RENDER_TIMEOUT}s."
	else
		err "flate render failed (exit ${status})."
		cat "${tmp_dir}/flate.stderr" >&2
	fi
	exit 1
fi

# 5. Print the path to the render. This is the only stdout output. The
#    render file must outlive the temp-dir cleanup below, so copy it to a
#    location that will not be removed on exit.
out_file="$(mktemp --suffix=-flate-render.yaml)"
cp "${render_file}" "${out_file}"
echo "${out_file}"
