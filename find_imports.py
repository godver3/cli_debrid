import os
import ast
import importlib.util

def get_python_files(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                yield os.path.join(root, file)

def get_imports(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        try:
            tree = ast.parse(file.read())
        except SyntaxError:
            print(f"Syntax error in {file_path}")
            return []

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module if node.module else ''
            for alias in node.names:
                imports.add(f"{module}.{alias.name}")

    return imports

def is_local_module(module_name, base_path):
    parts = module_name.split('.')
    current_path = base_path
    for part in parts:
        current_path = os.path.join(current_path, part)
        if os.path.isfile(current_path + '.py') or os.path.isdir(current_path):
            continue
        return False
    return True

def main():
    base_path = os.path.dirname(os.path.abspath(__file__))  # Assumes this script is in the project root
    all_imports = set()

    for file_path in get_python_files(base_path):
        all_imports.update(get_imports(file_path))

    local_imports = {imp for imp in all_imports if is_local_module(imp, base_path)}

    print("Potential hidden imports:")
    for imp in sorted(local_imports):
        print(f"--hidden-import={imp}")

if __name__ == "__main__":
    main()