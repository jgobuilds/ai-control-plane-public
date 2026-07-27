# Brightworks installer (wrapper) — see scripts/install.py for options.
#   .\install.ps1           interactive
#   .\install.ps1 --up      accept defaults, then build + start
param([Parameter(ValueFromRemainingArguments = $true)] $Rest)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
python "$root\scripts\install.py" @Rest
