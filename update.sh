#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REMOTE_NAME=${EDGE_DEPLOY_REMOTE:-${DISPATCH_UPDATE_REMOTE:-${GIT_REMOTE:-bitbucket}}}
BRANCH_NAME=${EDGE_DEPLOY_BRANCH:-${DISPATCH_UPDATE_BRANCH:-${GIT_BRANCH:-main}}}
REMOTE_REF="refs/remotes/$REMOTE_NAME/$BRANCH_NAME"
TARGET_REF=${1:-$REMOTE_REF}

cd "$ROOT_DIR"
CURRENT_HEAD=$(git rev-parse HEAD 2>/dev/null || true)

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "$ROOT_DIR is not a Git working tree" >&2
  exit 1
fi

FETCH_OUTPUT=$(git fetch --prune "$REMOTE_NAME" "$BRANCH_NAME:$REMOTE_REF" 2>&1) || {
  FETCH_STATUS=$?
  case "$FETCH_OUTPUT" in
    *"cannot lock ref '$REMOTE_REF'"*)
      echo "Detected stale remote-tracking ref at $REMOTE_REF; repairing and retrying..." >&2
      git update-ref -d "$REMOTE_REF"
      git fetch --prune "$REMOTE_NAME" "$BRANCH_NAME:$REMOTE_REF"
      ;;
    *)
      printf '%s\n' "$FETCH_OUTPUT" >&2
      exit "$FETCH_STATUS"
      ;;
  esac
}
CHANGED_FILES=""
if [ -n "$CURRENT_HEAD" ] && git rev-parse --verify "$TARGET_REF" >/dev/null 2>&1; then
  CHANGED_FILES=$(git diff --name-only "$CURRENT_HEAD" "$TARGET_REF" 2>/dev/null || true)
fi
git reset --hard "$TARGET_REF"

# Keep the shared tree readable/traversable for analysts. Git restores tracked
# executable bits during reset; a+rX preserves those bits while fixing directory
# traversal and read permissions. Untracked runtime/vendor files are preserved.
chmod 755 "$ROOT_DIR"
while IFS= read -r _path; do
  [ -n "$_path" ] || continue
  [ -e "$_path" ] && chmod a+r "$_path" 2>/dev/null || true
  _parent=$(dirname "$_path")
  while [ "$_parent" != "." ] && [ "$_parent" != "/" ]; do
    chmod a+rx "$_parent" 2>/dev/null || true
    _parent=$(dirname "$_parent")
  done
done <<EOF
$CHANGED_FILES
EOF
chmod a+rx . update.sh install.sh 2>/dev/null || true

# Shared usage telemetry rollup (append-only per-user JSONL). Sticky bit so
# analysts can create their own file without deleting others'.
TELEMETRY_USERS="$ROOT_DIR/telemetry/users"
mkdir -p "$TELEMETRY_USERS"
chmod 755 "$ROOT_DIR/telemetry" 2>/dev/null || true
chmod 1777 "$TELEMETRY_USERS" 2>/dev/null || true

echo "Dispatch shared tree updated:"
echo "  path:   $ROOT_DIR"
echo "  remote: $REMOTE_NAME"
echo "  branch: $BRANCH_NAME"
echo "  commit: $(git rev-parse --short HEAD)"
