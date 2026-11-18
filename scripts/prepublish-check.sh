#!/usr/bin/env bash
# Read-only checks to run before pushing NarSat to a public repository.
#
# Lists what `git add --all` would commit and reports:
#   1. files that must never be committed (.env, virtual environments, data, keys, OS files),
#   2. files larger than 1 MB,
#   3. secret values from .env (variables named *KEY*, *SECRET*, *PASSWORD*, *TOKEN*)
#      appearing in those files,
#   4. common credential patterns (AWS keys, private keys, GitHub and Hugging Face tokens),
#   5. absolute home-directory paths.
#
# It prints file and variable names, never the secret values. Exit status 1 if anything
# needs fixing. Usage, from the repository root after `git init`:
#   bash scripts/prepublish-check.sh
set -uo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "Not inside a git repository: run 'git init -b main' first." >&2
  exit 2
}
cd "$root" || exit 2

list=$(mktemp)
trap 'rm -f "$list"' EXIT
git add --dry-run --all . | sed -n "s/^add '\(.*\)'\$/\1/p" > "$list"

status=0
echo "Files that would be committed: $(wc -l < "$list" | tr -d ' ')"
echo "By top-level folder:"
awk -F/ '{ print (NF > 1 ? $1 "/" : $1) }' "$list" | sort | uniq -c | sed 's/^/  /'

indent() {  # prefix every line of standard input with $1
  local text
  while IFS= read -r text; do printf '%s%s\n' "$1" "$text"; done
}

files_matching() {  # grep options and pattern; prints the committed files that match
  tr '\n' '\0' < "$list" | xargs -0 grep -l -I "$@" 2>/dev/null
}

echo
echo "1. Files that must never be committed"
bad=$(grep -E '(^|/)\.env$|(^|/)\.env\.(local|prod|production|dev|test)$|(^|/)\.venv/|node_modules/|(^|/)var/|\.(tif|tiff|parquet|pem|key|p12)$|passwords\.json$|\.DS_Store$|\.(orig|rej)$' "$list")
if [ -n "$bad" ]; then
  printf '%s\n' "$bad" | indent "   "
  status=1
else
  echo "   none"
fi

echo
echo "2. Files larger than 1 MB"
big=0
while IFS= read -r file; do
  size=$(wc -c < "$file" | tr -d ' ')
  if [ "$size" -gt 1000000 ]; then
    echo "   $file ($size bytes)"
    big=1
  fi
done < "$list"
[ "$big" -eq 0 ] && echo "   none"

echo
echo "3. Secret values from .env found in the committed files"
found=0
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    name=${line%%=*}
    value=${line#*=}
    value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
    case "$name" in *KEY* | *SECRET* | *PASSWORD* | *TOKEN*) ;; *) continue ;; esac
    [ "${#value}" -ge 8 ] || continue
    hits=$(files_matching -F -- "$value")
    if [ -n "$hits" ]; then
      echo "   the value of $name appears in:"
      printf '%s\n' "$hits" | indent "      "
      found=1
      status=1
    fi
  done < .env
else
  echo "   (no .env file here, nothing to compare)"
fi
[ "$found" -eq 0 ] && echo "   none"

echo
echo "4. Common credential patterns"
hits=$(files_matching -E -- 'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[0-9A-Za-z]{36}|github_pat_[0-9A-Za-z_]{40,}|hf_[0-9A-Za-z]{30,}')
if [ -n "$hits" ]; then
  printf '%s\n' "$hits" | indent "   "
  status=1
else
  echo "   none"
fi

echo
echo "5. Absolute home-directory paths"
hits=$(files_matching -E -- '/Users/[A-Za-z0-9._-]+/|/home/[a-z][A-Za-z0-9._-]*/')
if [ -n "$hits" ]; then
  printf '%s\n' "$hits" | indent "   "
  status=1
else
  echo "   none"
fi

echo
if [ "$status" -eq 0 ]; then
  echo "RESULT: nothing to fix."
else
  echo "RESULT: fix the items above before committing."
fi
exit "$status"
