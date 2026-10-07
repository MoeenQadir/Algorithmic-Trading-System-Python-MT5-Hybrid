# Security scan for exposed secrets in source files
# Exclude common directories and file types that shouldn't be scanned

$excludeDirs = @(
    "\.venv",
    "node_modules", 
    ".git",
    "dist",
    "__pycache__",
    ".pytest_cache",
    "bin",
    "obj"
)

$excludeExtensions = @(
    ".exe", ".dll", ".so", ".bin",  # Binary files
    ".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg",  # Images
    ".pdf", ".zip", ".tar", ".gz",  # Archives
    ".pyc", ".pyo", ".pyd",  # Python compiled
    ".class", ".jar",  # Java
    ".lock",  # Lock files
    ".log",  # Log files
    ".min.js", ".min.css"  # Minified files
)

# Simple patterns to search for (case-insensitive)
$patterns = @(
    "api[_-]?key",
    "apikey",
    "secret[_-]?key",
    "secret",
    "password",
    "token",
    "credential",
    "private[_-]?key",
    "auth[_-]?token",
    "access[_-]?token",
    "refresh[_-]?token",
    "sk_live_",
    "sk_test_",
    "AKIA",
    "sg\\.",
    "Bearer ",
    "ghp_",
    "glpat-"
)

# Get all files in the project
$files = Get-ChildItem -Path "E:\Trading Framework" -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $fullPath = $_.FullName.ToLower()
        $fileName = $_.Name.ToLower()
        
        # Check if file is in excluded directories
        $inExcludeDir = $false
        foreach ($dir in $excludeDirs) {
            if ($fullPath -like "*$dir*") {
                $inExcludeDir = $true
                break
            }
        }
        if ($inExcludeDir) { return $false }
        
        # Check if file has excluded extension
        $extension = [System.IO.Path]::GetExtension($fileName)
        if ($excludeExtensions -contains $extension) {
            return $false
        }
        
        return $true
    }

# For each file, try to read as text and search for patterns
$results = @()
foreach ($file in $files) {
    try {
        # Skip very large files (> 1MB)
        if ($file.Length -gt 1MB) { continue }
        
        # Read file as text
        $content = Get-Content -Path $file.FullName -Raw -ErrorAction Stop
        if (-not $content) { continue }
        
        foreach ($pattern in $patterns) {
            # Use simple string search for these patterns
            $index = 0
            while (($index = $content.IndexOf($pattern, $index, [System.StringComparison]::OrdinalIgnoreCase)) -ge 0) {
                # Get context around the match (50 chars before and after)
                $length = $pattern.Length
                $start = [Math]::Max(0, $index - 50)
                $end = [Math]::Min($content.Length, $index + $length + 50)
                $context = $content.Substring($start, $end - $start)
                
                $results += [PSCustomObject]@{
                    File = $file.FullName.Substring((Get-Location).Path.Length + 1)  # Relative path
                    LineNumber = ($content.Substring(0, $index) -split "`n").Length + 1
                    Pattern = $pattern
                    Match = $content.Substring($index, $length)
                    Context = $context
                }
                
                $index += $length
            }
        }
    } catch {
        # Skip files that can't be read as text
        continue
    }
}

# Output results
if ($results) {
    Write-Host "Found $($results.Count) potential secret matches:" -ForegroundColor Yellow
    $results | Sort-Object File, LineNumber | Format-Table -AutoSize -Wrap
} else {
    Write-Host "No potential secrets found in source files." -ForegroundColor Green
}