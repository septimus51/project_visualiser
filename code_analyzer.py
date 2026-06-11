#!/usr/bin/env python3
"""
Code Dependency Analyzer
Analyse les dépendances entre fichiers d'un projet de code.
Produit un JSON représentant le graphe d'appels et un fichier de rapports d'erreurs.
"""

import os
import sys
import json
import re
import ast
import argparse
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass, field, asdict


@dataclass
class FunctionCall:
    """Représente un appel de fonction."""
    name: str
    source_file: str
    source_function: Optional[str]
    line: int
    column: int
    is_external: bool = False  # True si la fonction vient d'un import


@dataclass
class FileImport:
    """Représente un import dans un fichier."""
    module: str
    names: List[str] = field(default_factory=list)
    is_relative: bool = False
    line: int = 0
    source: str = ""  # La ligne d'import originale
    is_include: bool = False  # True pour #include C/C++


@dataclass
class FunctionDef:
    """Représente une définition de fonction."""
    name: str
    line_start: int
    line_end: int
    calls: List[FunctionCall] = field(default_factory=list)


@dataclass
class FileAnalysis:
    """Résultat d'analyse d'un fichier."""
    path: str
    language: str
    functions: List[FunctionDef] = field(default_factory=list)
    imports: List[FileImport] = field(default_factory=list)
    classes: List[str] = field(default_factory=list)
    includes: List[FileImport] = field(default_factory=list)  # Spécifique C/C++
    success: bool = True
    error: Optional[str] = None


class PythonASTAnalyzer(ast.NodeVisitor):
    """Analyseur AST pour les fichiers Python."""
    
    def __init__(self, file_path: str, source_code: str):
        self.file_path = file_path
        self.source_code = source_code
        self.lines = source_code.split('\n')
        self.current_function: Optional[str] = None
        self.function_stack: List[str] = []
        self.functions: List[FunctionDef] = []
        self.imports: List[FileImport] = []
        self.classes: List[str] = []
        self.imported_names: Dict[str, str] = {}  # nom -> module source
        
    def analyze(self) -> FileAnalysis:
        try:
            tree = ast.parse(self.source_code)
            self.visit(tree)
            return FileAnalysis(
                path=self.file_path,
                language="python",
                functions=self.functions,
                imports=self.imports,
                classes=self.classes,
                success=True
            )
        except SyntaxError as e:
            return FileAnalysis(
                path=self.file_path,
                language="python",
                success=False,
                error=f"Syntax error at line {e.lineno}: {e.msg}"
            )
        except Exception as e:
            return FileAnalysis(
                path=self.file_path,
                language="python",
                success=False,
                error=str(e)
            )
    
    def visit_FunctionDef(self, node):
        func_def = FunctionDef(
            name=node.name,
            line_start=node.lineno,
            line_end=getattr(node, 'end_lineno', node.lineno)
        )
        self.functions.append(func_def)
        self.function_stack.append(node.name)
        self.current_function = node.name
        self.generic_visit(node)
        self.function_stack.pop()
        self.current_function = self.function_stack[-1] if self.function_stack else None
    
    visit_AsyncFunctionDef = visit_FunctionDef
    
    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        # Les méthodes de classe sont aussi des functions
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_name = f"{node.name}.{item.name}"
                func_def = FunctionDef(
                    name=method_name,
                    line_start=item.lineno,
                    line_end=getattr(item, 'end_lineno', item.lineno)
                )
                self.functions.append(func_def)
                self.function_stack.append(method_name)
                self.current_function = method_name
                self.visit(item)
                self.function_stack.pop()
                self.current_function = self.function_stack[-1] if self.function_stack else None
            else:
                self.visit(item)
    
    def visit_Call(self, node):
        func_name = self._get_call_name(node.func)
        if func_name:
            is_external = func_name.split('.')[0] in self.imported_names
            
            call = FunctionCall(
                name=func_name,
                source_file=self.file_path,
                source_function=self.current_function,
                line=node.lineno,
                column=node.col_offset,
                is_external=is_external
            )
            
            # Ajouter l'appel à la fonction courante
            if self.current_function:
                for func in self.functions:
                    if func.name == self.current_function:
                        func.calls.append(call)
                        break
            else:
                # Appel au niveau module
                if self.functions:
                    # On pourrait créer une fonction "__module__" implicite
                    pass
        
        self.generic_visit(node)
    
    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            self.imported_names[name] = alias.name
            self.imports.append(FileImport(
                module=alias.name,
                names=[alias.name],
                line=node.lineno,
                source=self.lines[node.lineno - 1].strip()
            ))
    
    def visit_ImportFrom(self, node):
        module = node.module or ""
        names = []
        for alias in node.names:
            import_name = alias.asname if alias.asname else alias.name
            self.imported_names[import_name] = f"{module}.{alias.name}" if module else alias.name
            names.append(alias.name)
        
        self.imports.append(FileImport(
            module=module,
            names=names,
            is_relative=node.level > 0,
            line=node.lineno,
            source=self.lines[node.lineno - 1].strip()
        ))
    
    def _get_call_name(self, node) -> Optional[str]:
        """Extrait le nom complet d'un appel de fonction."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            value = self._get_call_name(node.value)
            return f"{value}.{node.attr}" if value else node.attr
        elif isinstance(node, ast.Subscript):
            return self._get_call_name(node.value)
        return None


class CIncludeAnalyzer:
    """Analyseur spécialisé pour les #include C/C++."""
    
    # Pattern pour #include avec détection du type (<> ou "")
    INCLUDE_PATTERN = re.compile(
        r'^\s*#\s*include\s+(<([^>]+)>|"([^"]+)")',
        re.MULTILINE
    )
    
    # Pattern pour les définitions de fonctions C/C++
    FUNCTION_PATTERN = re.compile(
        r'(?:^|\n)\s*(?:inline\s+|static\s+|extern\s+|virtual\s+|'
        r'[\w\*&:<>,\s]+?\s+)*?'
        r'(\w+)\s*\([^)]*\)\s*(?:const\s*|override\s*|final\s*|noexcept\s*)*\s*\{',
        re.MULTILINE
    )
    
    # Pattern pour les appels de fonction C/C++
    CALL_PATTERN = re.compile(
        r'(\b\w+\b)\s*\(',
        re.MULTILINE
    )
    
    # Pattern pour les classes/structs
    CLASS_PATTERN = re.compile(
        r'(?:class|struct|enum|union|namespace)\s+(\w+)',
        re.MULTILINE
    )
    
    def __init__(self, file_path: str, source_code: str, language: str = 'c'):
        self.file_path = file_path
        self.source_code = source_code
        self.language = language
        self.lines = source_code.split('\n')
        
    def analyze(self) -> FileAnalysis:
        try:
            includes = self._extract_includes()
            functions = self._extract_functions()
            classes = self._extract_classes()
            
            return FileAnalysis(
                path=self.file_path,
                language=self.language,
                functions=functions,
                imports=includes,  # Les includes sont aussi des imports
                classes=classes,
                includes=includes,  # Spécifique pour C/C++
                success=True
            )
        except Exception as e:
            return FileAnalysis(
                path=self.file_path,
                language=self.language,
                success=False,
                error=str(e)
            )
    
    def _extract_includes(self) -> List[FileImport]:
        """Extrait les #include avec distinction <> et ""."""
        includes = []
        
        for match in self.INCLUDE_PATTERN.finditer(self.source_code):
            # match.group(1) = <file> ou "file"
            # match.group(2) = file sans <> (si <>)
            # match.group(3) = file sans "" (si "")
            
            if match.group(2):  # #include <...> = système
                include_path = match.group(2)
                is_system = True
            else:  # #include "..." = local/projet
                include_path = match.group(3)
                is_system = False
            
            # Trouver la ligne
            line_num = self.source_code[:match.start()].count('\n') + 1
            
            includes.append(FileImport(
                module=include_path,
                names=[include_path],
                is_relative=not is_system,  # "" = relatif/local, <> = système
                line=line_num,
                source=self.lines[line_num - 1].strip() if line_num <= len(self.lines) else f'#include <{include_path}>' if is_system else f'#include "{include_path}"',
                is_include=True
            ))
        
        return includes
    
    def _extract_functions(self) -> List[FunctionDef]:
        """Extrait les définitions de fonctions C/C++."""
        functions = []
        
        for match in self.FUNCTION_PATTERN.finditer(self.source_code):
            func_name = match.group(1)
            # Ignorer les mots-clés C/C++
            if func_name in ['if', 'while', 'for', 'switch', 'catch', 'return', 
                            'sizeof', 'new', 'delete', 'static_cast', 'dynamic_cast',
                            'const_cast', 'reinterpret_cast', 'decltype', 'typeof']:
                continue
            
            line_num = self.source_code[:match.start()].count('\n') + 1
            
            # Chercher les appels dans la fonction
            calls = self._find_calls_in_function(match.start(), match.end())
            
            functions.append(FunctionDef(
                name=func_name,
                line_start=line_num,
                line_end=line_num,  # On ne calcule pas la fin exacte ici
                calls=calls
            ))
        
        return functions
    
    def _find_calls_in_function(self, start_pos: int, end_pos: int) -> List[FunctionCall]:
        """Trouve les appels de fonction dans une fonction donnée."""
        calls = []
        func_body = self.source_code[start_pos:end_pos]
        
        for match in self.CALL_PATTERN.finditer(func_body):
            call_name = match.group(1)
            # Ignorer les mots-clés et les appels évidents
            if call_name in ['if', 'while', 'for', 'switch', 'catch', 'return',
                            'sizeof', 'new', 'delete', 'static_cast', 'dynamic_cast',
                            'const_cast', 'reinterpret_cast']:
                continue
            
            # Calculer la ligne relative au fichier
            line_offset = self.source_code[:start_pos].count('\n')
            local_line = func_body[:match.start()].count('\n')
            absolute_line = line_offset + local_line + 1
            
            calls.append(FunctionCall(
                name=call_name,
                source_file=self.file_path,
                source_function=None,  # Sera mis à jour
                line=absolute_line,
                column=match.start() - func_body.rfind('\n', 0, match.start()) if '\n' in func_body[:match.start()] else match.start(),
                is_external=False  # Sera déterminé plus tard
            ))
        
        return calls
    
    def _extract_classes(self) -> List[str]:
        """Extrait les classes/structs/enum/union/namespace."""
        classes = []
        for match in self.CLASS_PATTERN.finditer(self.source_code):
            classes.append(match.group(1))
        return classes


class GenericRegexAnalyzer:
    """Analyseur par regex pour les langages non supportés nativement."""
    
    # Patterns par langage
    PATTERNS = {
        'javascript': {
            'function': r'(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:function|\(.*?\)\s*=>)|(\w+)\s*:\s*function)',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'(?:import\s+.*?from\s+[\'"]([^\'"]+)[\'"]|require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\))',
            'class': r'class\s+(\w+)'
        },
        'typescript': {
            'function': r'(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*[:=]\s*(?:function|\(.*?\)\s*=>)|(\w+)\s*:\s*function|(\w+)\s*\(.*?\)\s*:\s*\w+\s*\{)',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'(?:import\s+.*?from\s+[\'"]([^\'"]+)[\'"]|require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\))',
            'class': r'(?:class|interface)\s+(\w+)'
        },
        'java': {
            'function': r'(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*\{',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'import\s+([\w.]+(?:\.\*)?);',
            'class': r'(?:class|interface|enum)\s+(\w+)'
        },
        'go': {
            'function': r'func\s+(?:\(\w+\s+[\w\*]+\)\s+)?(\w+)\s*\([^)]*\)',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'import\s+(?:\(\s*)?(?:"([^"]+)"|\'([^\']+)\')',
            'class': None
        },
        'ruby': {
            'function': r'(?:def\s+(?:self\.)?(\w+)|(\w+)\s*=\s*(?:lambda|proc))',
            'call': r'(\w+(?:\.\w+)*)\s*(?:\(|$)',
            'import': r'require\s*[\'"]([^\'"]+)[\'"]',
            'class': r'class\s+(\w+)'
        },
        'php': {
            'function': r'(?:function\s+(\w+)|(\w+)\s*=\s*function)',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'(?:require|include)(?:_once)?\s*\(?\s*[\'"]([^\'"]+)[\'"]',
            'class': r'class\s+(\w+)'
        },
        'rust': {
            'function': r'fn\s+(\w+)\s*\(',
            'call': r'(\w+(?:::\w+)*)\s*\(',
            'import': r'use\s+([\w:]+);',
            'class': r'(?:struct|enum|trait|impl)\s+(?:<\w+>\s+)?(\w+)'
        },
        'csharp': {
            'function': r'(?:public|private|protected|internal|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*\{',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'using\s+([\w.]+);',
            'class': r'(?:class|interface|struct)\s+(\w+)'
        },
        'kotlin': {
            'function': r'fun\s+(\w+)\s*\(',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'import\s+([\w.]+)',
            'class': r'(?:class|interface|object)\s+(\w+)'
        },
        'swift': {
            'function': r'func\s+(\w+)\s*\(',
            'call': r'(\w+(?:\.\w+)*)\s*\(',
            'import': r'import\s+([\w.]+)',
            'class': r'(?:class|struct|enum|protocol)\s+(\w+)'
        },
        'shell': {
            'function': r'(\w+)\s*\(\s*\)\s*\{',
            'call': r'(\w+)(?:\s|$)',
            'import': r'source\s+["\']?([^"\']+)["\']?',
            'class': None
        }
    }
    
    def __init__(self, file_path: str, source_code: str, language: str):
        self.file_path = file_path
        self.source_code = source_code
        self.language = language
        self.patterns = self.PATTERNS.get(language, self.PATTERNS['javascript'])
    
    def analyze(self) -> FileAnalysis:
        try:
            functions = self._extract_functions()
            imports = self._extract_imports()
            classes = self._extract_classes()
            
            return FileAnalysis(
                path=self.file_path,
                language=self.language,
                functions=functions,
                imports=imports,
                classes=classes,
                success=True
            )
        except Exception as e:
            return FileAnalysis(
                path=self.file_path,
                language=self.language,
                success=False,
                error=str(e)
            )
    
    def _extract_functions(self) -> List[FunctionDef]:
        functions = []
        lines = self.source_code.split('\n')
        
        # Extraction simple des fonctions
        for i, line in enumerate(lines, 1):
            matches = re.finditer(self.patterns['function'], line)
            for match in matches:
                name = next((g for g in match.groups() if g), "unknown")
                # Chercher les appels dans le bloc suivant (heuristique simple)
                calls = self._find_calls_in_scope(i, lines)
                functions.append(FunctionDef(
                    name=name,
                    line_start=i,
                    line_end=i,
                    calls=calls
                ))
        
        return functions
    
    def _find_calls_in_scope(self, start_line: int, lines: List[str]) -> List[FunctionCall]:
        """Heuristique pour trouver les appels dans la portée d'une fonction."""
        calls = []
        brace_count = 0
        in_function = False
        
        for i in range(start_line - 1, min(start_line + 200, len(lines))):
            line = lines[i]
            
            if not in_function:
                if '{' in line or (self.language in ['python', 'ruby'] and ':' in line):
                    in_function = True
            
            if in_function:
                brace_count += line.count('{') - line.count('}')
                
                for match in re.finditer(self.patterns['call'], line):
                    call_name = match.group(1)
                    if call_name not in ['if', 'while', 'for', 'switch', 'catch', 'sizeof', 'return', 'new']:
                        calls.append(FunctionCall(
                            name=call_name,
                            source_file=self.file_path,
                            source_function=None,  # Sera mis à jour après
                            line=i + 1,
                            column=match.start()
                        ))
                
                if brace_count <= 0 and '{' in lines[start_line - 1:i + 1]:
                    break
        
        return calls
    
    def _extract_imports(self) -> List[FileImport]:
        imports = []
        for line in self.source_code.split('\n'):
            matches = re.finditer(self.patterns['import'], line)
            for match in matches:
                module = next((g for g in match.groups() if g), "")
                imports.append(FileImport(
                    module=module,
                    line=0,
                    source=line.strip()
                ))
        return imports
    
    def _extract_classes(self) -> List[str]:
        if not self.patterns['class']:
            return []
        classes = []
        for line in self.source_code.split('\n'):
            match = re.search(self.patterns['class'], line)
            if match:
                classes.append(match.group(1))
        return classes


class DependencyGraphBuilder:
    """Construit le graphe de dépendances complet."""
    
    LANGUAGE_MAP = {
        '.py': 'python',
        '.js': 'javascript',
        '.jsx': 'javascript',
        '.ts': 'typescript',
        '.tsx': 'typescript',
        '.java': 'java',
        '.c': 'c',
        '.h': 'c',
        '.cpp': 'cpp',
        '.hpp': 'cpp',
        '.cc': 'cpp',
        '.go': 'go',
        '.rb': 'ruby',
        '.php': 'php',
        '.rs': 'rust',
        '.cs': 'csharp',
        '.kt': 'kotlin',
        '.swift': 'swift',
        '.sh': 'shell',
        '.bash': 'shell',
        '.zsh': 'shell'
    }
    
    def __init__(self, project_path: str):
        self.project_path = Path(project_path).resolve()
        self.analyses: List[FileAnalysis] = []
        self.failed_files: List[Tuple[str, str]] = []
        self.file_index: Dict[str, FileAnalysis] = {}
        self.function_index: Dict[str, List[str]] = defaultdict(list)  # func_name -> [file_paths]
        self.include_index: Dict[str, List[str]] = defaultdict(list)  # nom_fichier -> [chemins]
        
    def discover_files(self) -> List[Path]:
        """Découvre tous les fichiers de code du projet."""
        files = []
        exclude_dirs = {'.git', '.svn', 'node_modules', '__pycache__', '.pytest_cache', 
                       'venv', '.venv', 'env', 'dist', 'build', '.idea', '.vscode',
                       'target', 'vendor', '.bundle', 'coverage', '.tox'}
        
        for path in self.project_path.rglob('*'):
            if path.is_file():
                # Ignorer les répertoires exclus
                if any(part in exclude_dirs for part in path.parts):
                    continue
                
                # Ignorer les fichiers binaires et non-code
                if path.suffix.lower() in self.LANGUAGE_MAP or path.suffix.lower() in ['.sql', '.r', '.m', '.scala', '.groovy', '.pl', '.pm']:
                    files.append(path)
                elif path.suffix == '' and path.stat().st_size < 100000:
                    # Fichiers sans extension, vérifier si c'est du code
                    try:
                        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                            sample = f.read(1024)
                            if any(keyword in sample for keyword in ['#!/bin/bash', '#!/usr/bin/env', 'def ', 'function', 'import ', 'from ']):
                                files.append(path)
                    except:
                        pass
        
        return sorted(files)
    
    def detect_language(self, file_path: Path) -> Optional[str]:
        """Détecte le langage d'un fichier."""
        if file_path.suffix.lower() in self.LANGUAGE_MAP:
            return self.LANGUAGE_MAP[file_path.suffix.lower()]
        
        # Détection par shebang
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                first_line = f.readline()
                if 'python' in first_line:
                    return 'python'
                elif 'node' in first_line or 'bash' in first_line or 'sh' in first_line:
                    return 'shell'
                elif 'ruby' in first_line:
                    return 'ruby'
        except:
            pass
        
        return None
    
    def analyze_file(self, file_path: Path) -> Optional[FileAnalysis]:
        """Analyse un fichier individuel."""
        rel_path = str(file_path.relative_to(self.project_path))
        language = self.detect_language(file_path)
        
        if not language:
            self.failed_files.append((rel_path, "Langage non détecté"))
            return None
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                source_code = f.read()
        except Exception as e:
            self.failed_files.append((rel_path, f"Erreur de lecture: {e}"))
            return None
        
        # Vérifier si le fichier est binaire
        if '\0' in source_code:
            self.failed_files.append((rel_path, "Fichier binaire"))
            return None
        
        # Analyse selon le langage
        if language == 'python':
            analyzer = PythonASTAnalyzer(rel_path, source_code)
        elif language in ['c', 'cpp']:
            analyzer = CIncludeAnalyzer(rel_path, source_code, language)
        else:
            analyzer = GenericRegexAnalyzer(rel_path, source_code, language)
        
        result = analyzer.analyze()
        
        if not result.success:
            self.failed_files.append((rel_path, result.error or "Erreur d'analyse inconnue"))
        
        return result
    
    def build_graph(self) -> Dict[str, Any]:
        """Construit le graphe JSON final."""
        # Indexer les fonctions et les includes
        for analysis in self.analyses:
            for func in analysis.functions:
                self.function_index[func.name].append(analysis.path)
            
            # Indexer les includes C/C++ pour résolution rapide
            if analysis.language in ['c', 'cpp']:
                for inc in analysis.includes:
                    # Indexer par nom de fichier (sans chemin)
                    file_name = Path(inc.module).name
                    self.include_index[file_name].append(analysis.path)
        
        # Construire les nœuds et liens
        nodes = []
        links = []
        file_nodes = {}
        
        # Nœuds fichiers
        for analysis in self.analyses:
            file_node = {
                "id": analysis.path,
                "type": "file",
                "language": analysis.language,
                "functions": [f.name for f in analysis.functions],
                "classes": analysis.classes
            }
            nodes.append(file_node)
            file_nodes[analysis.path] = file_node
        
        # Nœuds fonctions et liens d'appel
        for analysis in self.analyses:
            for func in analysis.functions:
                func_id = f"{analysis.path}::{func.name}"
                nodes.append({
                    "id": func_id,
                    "type": "function",
                    "name": func.name,
                    "file": analysis.path,
                    "line_start": func.line_start,
                    "line_end": func.line_end
                })
                
                # Lien fichier -> fonction
                links.append({
                    "source": analysis.path,
                    "target": func_id,
                    "type": "contains"
                })
                
                # Liens d'appel
                for call in func.calls:
                    # Résoudre la cible de l'appel
                    target = self._resolve_call_target(call, analysis)
                    if target:
                        links.append({
                            "source": func_id,
                            "target": target,
                            "type": "calls",
                            "line": call.line,
                            "column": call.column,
                            "is_external": call.is_external
                        })
        
        # Liens d'import entre fichiers (Python, Java, etc.)
        for analysis in self.analyses:
            for imp in analysis.imports:
                target_file = self._resolve_import(imp, analysis)
                if target_file and target_file in file_nodes:
                    link_type = "includes" if imp.is_include else "imports"
                    links.append({
                        "source": analysis.path,
                        "target": target_file,
                        "type": link_type,
                        "module": imp.module,
                        "names": imp.names,
                        "is_system": not imp.is_relative if imp.is_include else None
                    })
        
        return {
            "project_path": str(self.project_path),
            "total_files_analyzed": len(self.analyses),
            "total_files_failed": len(self.failed_files),
            "nodes": nodes,
            "links": links,
            "statistics": {
                "total_functions": sum(len(a.functions) for a in self.analyses),
                "total_classes": sum(len(a.classes) for a in self.analyses),
                "languages": defaultdict(int)
            }
        }
    
    def _resolve_call_target(self, call: FunctionCall, source_analysis: FileAnalysis) -> Optional[str]:
        """Tente de résoudre la cible d'un appel de fonction."""
        call_name = call.name
        
        # 1. Fonction locale au fichier
        for func in source_analysis.functions:
            if func.name == call_name:
                return f"{source_analysis.path}::{func.name}"
        
        # 2. Méthode de classe locale
        if '.' in call_name:
            base_name = call_name.split('.')[0]
            for cls in source_analysis.classes:
                if base_name == cls or base_name == 'self':
                    method_name = call_name.split('.')[-1]
                    for func in source_analysis.functions:
                        if func.name == f"{cls}.{method_name}" or func.name == method_name:
                            return f"{source_analysis.path}::{func.name}"
        
        # 3. Fonction importée
        if call.is_external:
            first_part = call_name.split('.')[0]
            for imp in source_analysis.imports:
                if first_part in imp.names or first_part == imp.module.split('.')[-1]:
                    # Chercher dans les autres fichiers
                    for other_analysis in self.analyses:
                        if any(other_analysis.path.endswith(p) for p in 
                               [imp.module.replace('.', '/') + '.py',
                                imp.module.split('.')[-1] + '.py']):
                            for func in other_analysis.functions:
                                if func.name == call_name.split('.')[-1]:
                                    return f"{other_analysis.path}::{func.name}"
        
        # 4. Recherche globale (fallback)
        if call_name in self.function_index and len(self.function_index[call_name]) == 1:
            return f"{self.function_index[call_name][0]}::{call_name}"
        
        # Non résolu, retourner le nom comme nœud externe
        return f"external::{call_name}" if call.is_external else None
    
    def _resolve_import(self, imp: FileImport, source_analysis: FileAnalysis) -> Optional[str]:
        """Tente de résoudre un import vers un fichier du projet."""
        # Pour les includes C/C++ locaux (#include "file.h")
        if imp.is_include and imp.is_relative:
            # Chercher dans le même dossier et les sous-dossiers
            source_dir = Path(source_analysis.path).parent
            target_name = Path(imp.module).name
            
            # Chercher le fichier exact
            for analysis in self.analyses:
                if analysis.path.endswith(imp.module) or Path(analysis.path).name == target_name:
                    return analysis.path
            
            # Chercher dans le même dossier
            candidate = source_dir / imp.module
            if candidate.exists():
                return str(candidate.relative_to(self.project_path))
        
        # Pour les includes système C/C++ (#include <file.h>)
        # On ne résout pas les système, mais on pourrait chercher dans le projet
        if imp.is_include and not imp.is_relative:
            target_name = Path(imp.module).name
            for analysis in self.analyses:
                if Path(analysis.path).name == target_name:
                    return analysis.path
        
        # Pour les imports Python/JS/Java...
        module_path = imp.module.replace('.', '/')
        
        candidates = [
            f"{module_path}.py",
            f"{module_path}.js",
            f"{module_path}.ts",
            f"{module_path}/__init__.py",
            f"{module_path}/index.js",
            f"{module_path}/index.ts",
            f"{imp.module}.py",
            f"{imp.module}.js"
        ]
        
        # Pour les imports relatifs
        if imp.is_relative and not imp.is_include:
            source_dir = Path(source_analysis.path).parent
            for level in range(1, 5):  # Jusqu'à 4 niveaux de parent
                if level <= len(source_dir.parts):
                    candidates.append(str(source_dir / f"{'/'.join(['..'] * (level-1))}" / f"{module_path}.py").replace('/./', '/'))
        
        for candidate in candidates:
            clean_candidate = candidate.replace('/./', '/').replace('/../', '/')
            for analysis in self.analyses:
                if analysis.path.endswith(clean_candidate) or analysis.path == clean_candidate:
                    return analysis.path
        
        return None
    
    def run(self, output_json: str, output_errors: str):
        """Exécute l'analyse complète."""
        print(f"🔍 Analyse du projet: {self.project_path}")
        
        files = self.discover_files()
        print(f"📁 {len(files)} fichiers trouvés")
        
        for i, file_path in enumerate(files, 1):
            if i % 50 == 0 or i == len(files):
                print(f"   Progression: {i}/{len(files)} fichiers analysés...")
            
            analysis = self.analyze_file(file_path)
            if analysis and analysis.success:
                self.analyses.append(analysis)
                self.file_index[analysis.path] = analysis
        
        # Construction du graphe
        print("🕸️  Construction du graphe de dépendances...")
        graph = self.build_graph()
        
        # Mise à jour des statistiques de langage
        for analysis in self.analyses:
            graph["statistics"]["languages"][analysis.language] += 1
        
        # Sauvegarde JSON
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(graph, f, indent=2, ensure_ascii=False, default=str)
        
        # Sauvegarde des erreurs
        with open(output_errors, 'w', encoding='utf-8') as f:
            f.write(f"# Fichiers non traités - {self.project_path.name}\n")
            f.write(f"# Total: {len(self.failed_files)}\n\n")
            for path, error in sorted(self.failed_files):
                f.write(f"{path}\n")
                f.write(f"  Erreur: {error}\n\n")
        
        print(f"\n✅ Analyse terminée!")
        print(f"   📊 Fichiers analysés: {len(self.analyses)}")
        print(f"   ❌ Fichiers en échec: {len(self.failed_files)}")
        print(f"   📈 Graphe: {len(graph['nodes'])} nœuds, {len(graph['links'])} liens")
        print(f"   💾 Résultats sauvegardés dans: {output_json}")
        print(f"   📝 Erreurs sauvegardées dans: {output_errors}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyse les dépendances d'un projet de code et génère un graphe JSON."
    )
    parser.add_argument(
        "project_path",
        help="Chemin vers le dossier du projet à analyser"
    )
    parser.add_argument(
        "-o", "--output",
        default="dependency_graph.json",
        help="Nom du fichier JSON de sortie (défaut: dependency_graph.json)"
    )
    parser.add_argument(
        "-e", "--errors",
        default="failed_files.txt",
        help="Nom du fichier de rapports d'erreurs (défaut: failed_files.txt)"
    )
    
    args = parser.parse_args()
    
    if not os.path.isdir(args.project_path):
        print(f"❌ Erreur: '{args.project_path}' n'est pas un dossier valide.")
        sys.exit(1)
    
    builder = DependencyGraphBuilder(args.project_path)
    builder.run(args.output, args.errors)


if __name__ == "__main__":
    main()