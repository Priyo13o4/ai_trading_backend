import ast
import os
import glob
import sys

def analyze_file(filepath):
    with open(filepath, 'r') as f:
        source = f.read()
    
    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError as e:
        print(f"SyntaxError in {filepath}: {e}")
        return

    # A simple analysis to find undefined names
    # This is not perfect but can catch obvious issues
    
    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scopes = [set()]
            self.undefined = set()
            self.imports = set()
            self.in_function_def = False
        
        def visit_Import(self, node):
            for alias in node.names:
                self.scopes[-1].add(alias.asname or alias.name.split('.')[0])
            self.generic_visit(node)
            
        def visit_ImportFrom(self, node):
            for alias in node.names:
                self.scopes[-1].add(alias.asname or alias.name)
            self.generic_visit(node)
            
        def visit_FunctionDef(self, node):
            self.scopes[-1].add(node.name)
            self.scopes.append(set())
            for arg in node.args.args:
                self.scopes[-1].add(arg.arg)
            if node.args.vararg:
                self.scopes[-1].add(node.args.vararg.arg)
            if node.args.kwarg:
                self.scopes[-1].add(node.args.kwarg.arg)
            for kwonlyarg in node.args.kwonlyargs:
                self.scopes[-1].add(kwonlyarg.arg)
                
            self.generic_visit(node)
            self.scopes.pop()
            
        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)
            
        def visit_ClassDef(self, node):
            self.scopes[-1].add(node.name)
            self.scopes.append(set())
            self.generic_visit(node)
            self.scopes.pop()
            
        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.scopes[-1].add(target.id)
                elif isinstance(target, ast.Tuple) or isinstance(target, ast.List):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            self.scopes[-1].add(elt.id)
            self.generic_visit(node)
            
        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name):
                self.scopes[-1].add(node.target.id)
            self.generic_visit(node)
            
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                is_defined = False
                for scope in self.scopes:
                    if node.id in scope:
                        is_defined = True
                        break
                if not is_defined:
                    # Ignore builtins
                    if node.id not in dir(__builtins__):
                        self.undefined.add(node.id)
            elif isinstance(node.ctx, ast.Store):
                # Simple store
                self.scopes[-1].add(node.id)
            self.generic_visit(node)
            
        def visit_Global(self, node):
            for name in node.names:
                self.scopes[0].add(name)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    if visitor.undefined:
        print(f"Undefined variables in {filepath}: {visitor.undefined}")

print("Analyzing python files...")
for py_file in ['app/main.py'] + glob.glob('app/routes/*.py'):
    analyze_file(py_file)
print("Analysis complete.")
