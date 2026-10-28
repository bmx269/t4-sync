#!/bin/sh
# Double-click this on macOS to open the t4-sync control panel.
#
# Copy it into your project directory -- the one holding .t4/ -- so that the
# panel opens on that project. Finder runs it from wherever it sits, which is
# why this cds to its own location rather than trusting the working directory.
#
# First time only: macOS may refuse to run a downloaded file. Right-click it
# and choose Open, or run  chmod +x t4-app.command  in Terminal.
cd "$(dirname "$0")" || exit 1

if command -v t4 >/dev/null 2>&1; then
    exec t4 app
fi

# Not on PATH: fall back to the module, trying the usual interpreter names.
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c "import t4sync" 2>/dev/null; then
        exec "$py" -m t4sync app
    fi
done

echo "t4-sync is not installed for any Python on this machine."
echo "Install it by running install.sh from the t4-sync checkout, then"
echo "double-click this again."
echo
printf "Press return to close. "
read -r _
