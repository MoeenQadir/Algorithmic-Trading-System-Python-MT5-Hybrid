import os
import re

# Define directories to exclude
exclude_dirs = {'.venv', 'node_modules', '.git', 'dist', '__pycache__', '.pytest_cache', 'bin', 'obj'}

# Define file extensions to include
include_extensions = {'.py', '.js', '.ts', '.tsx', '.md', '.txt'}

# Pattern to search for (case-insensitive)
pattern = re.compile(r'(TODO|FIXME|HACK|XXX)', re.IGNORECASE)

todos = []

# Walk through the directory
for root, dirs, files in os.walk('E:\\Trading Framework'):
    # Modify dirs in-place to exclude directories we don't want to search
    dirs[:] = [d for d in dirs if d not in exclude_dirs]
    
    for file in files:
        # Check if file extension is in our include list
        if any(file.lower().endswith(ext) for ext in include_extensions):
            file_path = os.path.join(root, file)
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    lines = content.split('\n')
                    
                    for i, line in enumerate(lines, 1):  # Line numbers start at 1
                        if pattern.search(line):
                            # Get relative path from project root
                            rel_path = os.path.relpath(file_path, 'E:\\Trading Framework')
                            todos.append({
                                'file': rel_path,
                                'line': i,
                                'content': line.strip()
                            })
            except Exception as e:
                # Skip files that can't be read
                continue

if todos:
    print(f'Found {len(todos)} TODO/FIXME/HACK/XXX items:')
    for todo in sorted(todos, key=lambda x: (x['file'], x['line'])):
        print(f'{todo["file"]}:{todo["line"]}: {todo["content"]}')
else:
    print('No TODO/FIXME/HACK/XXX items found in source files.')