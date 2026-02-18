"""
File type classification for the local scanner.

Mirrors the server-side ``file_extensions.py`` logic so that the metadata
produced locally matches exactly what the CodeDD platform expects.

Categories:
    Source Code, Configuration, Documentation, Data File,
    Binary, Media, Archive, System, Security, Other
"""

import os

# ---------------------------------------------------------------------------
# Extension sets — kept in sync with the server-side definitions
# ---------------------------------------------------------------------------

CONFIGURATION_FILES = {
    '.yaml', '.yml', '.ini', '__init__.py', '.env', '.conf', '.config', '.cfg',
    '.properties', '.prefs', '.props', '.toml', '.cnf', '.json',
    '.dist', '.opt', '.plist', '.reg', '.settings', '.editorconfig',
    '.htaccess', '.htpasswd', '.npmrc', '.nvmrc', '.bowerrc',
    '.eslintrc', '.prettierrc', '.stylelintrc', '.babelrc',
    '.eslintignore', '.prettierignore', '.dockerignore', '.gitignore',
    '.env.local', '.env.development', '.env.production', '.env.test',
    'Dockerfile', 'docker-compose.yml', 'Makefile', 'CMakeLists.txt',
    'webpack.config.js', 'rollup.config.js', 'vite.config.js', 'snowpack.config.js',
    'tsconfig.json', 'package.json', 'composer.json', 'project.json',
    'angular.json', 'nx.json', 'lerna.json', 'rush.json',
    '.travis.yml', '.gitlab-ci.yml', '.github/workflows', 'Jenkinsfile',
    'azure-pipelines.yml', '.circleci/config.yml', 'bitbucket-pipelines.yml',
    'buildkite.yml', 'appveyor.yml', 'cloudbuild.yaml', 'codeship-services.yml',
    'setup.cfg', 'pyproject.toml', 'requirements.txt', 'Pipfile',
    'tox.ini', 'pytest.ini', '.coveragerc', '.flake8', 'poetry.lock',
    'conda.yaml', 'environment.yml', 'setup.py', 'MANIFEST.in',
    'docker-compose.override.yml', 'docker-compose.prod.yml', 'docker-compose.dev.yml',
    'kubernetes.yaml', 'helm.yaml', 'values.yaml', 'Chart.yaml', 'kustomization.yaml',
    'terraform.tfvars', 'terragrunt.hcl', 'serverless.yml', 'cloudformation.yaml',
    'pulumi.yaml', 'ansible.cfg', 'inventory.yml', 'playbook.yml',
    '.browserslistrc', '.postcssrc', '.cssnanorc', '.stylelintignore',
    'tailwind.config.js', 'next.config.js', 'nuxt.config.js', 'svelte.config.js',
    'remix.config.js', 'astro.config.mjs', '.parcelrc', 'gatsby-config.js',
    'gunicorn.conf.py', 'uwsgi.ini', 'nginx.conf', 'apache2.conf',
    'php.ini', 'my.cnf', 'postgresql.conf', 'redis.conf', 'supervisord.conf',
    'pm2.config.js', 'nodemon.json', 'nest-cli.json', 'vercel.json',
    '.nycrc', '.mocharc', '.jestrc', 'karma.conf.js', 'cypress.json',
    'playwright.config.js', 'sonar-project.properties', '.yamllint',
    'vitest.config.js', 'ava.config.js', 'wallaby.js', 'jasmine.json',
    '.vscode/settings.json', '.vscode/launch.json', '.idea/workspace.xml',
    '.eclipse/org.eclipse.jdt.core.prefs', '.netbeans/project.properties',
}

SOURCE_CODE_FILES = {
    '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.php', '.html', '.htm',
    '.css', '.scss', '.sass', '.less', '.styl', '.wasm', '.mjs', '.cjs',
    '.astro', '.mdx', '.razor', '.cshtml', '.jsp', '.asp', '.aspx',
    '.php4', '.php5', '.phtml', '.ctp', '.module', '.inc.php',
    '.d.ts', '.js.map', '.mts', '.cts', '.jsx.map', '.tsx.map',
    '.es6', '.es', '.iife.js', '.umd.js', '.amd.js', '.esm.js',
    '.py', '.java', '.cpp', '.cc', '.cxx', '.hpp', '.c', '.h', '.cs', '.fs',
    '.go', '.rs', '.rb', '.rake', '.swift', '.kt', '.kts', '.scala', '.sc',
    '.clj', '.cljs', '.cljc', '.erl', '.ex', '.exs', '.hs', '.lhs', '.lua',
    '.pl', '.pm', '.t', '.r', '.rmd', '.jl', '.dart', '.groovy', '.tcl',
    '.nim', '.cr', '.ml', '.re', '.res', '.elm', '.zig', '.v', '.gleam',
    '.hx', '.ceylon', '.idr', '.purs', '.dhall', '.bal', '.rkt', '.io',
    '.sh', '.bash', '.zsh', '.fish', '.bat', '.cmd', '.ps1', '.psm1',
    '.vbs', '.vba', '.awk', '.sed', '.ksh', '.csh', '.tcsh', '.nu',
    '.erb', '.haml', '.slim', '.pug', '.jade', '.jinja', '.jinja2',
    '.mustache', '.handlebars', '.hbs', '.twig', '.liquid', '.njk',
    '.blade.php', '.volt', '.latte', '.smarty', '.plates', '.tpl',
    '.sql', '.hql', '.cypher', '.graphql', '.gql', '.proto', '.thrift',
    '.cmake', '.gradle', '.m4', '.am', '.in', '.ac', '.f90', '.f95', '.f03',
    '.m', '.mm', '.xib', '.storyboard',
    '.gd', '.unity', '.unityproj', '.prefab', '.mat', '.shader',
    '.hlsl', '.cg', '.fx', '.fxh', '.usf', '.ush',
    '.ipynb', '.sage', '.sce', '.sci', '.stan', '.do',
    '.sps', '.sas', '.mlx', '.mplstyle',
    '.py3', '.pyx', '.pxd', '.pxi', '.rpy', '.rpym', '.rviz',
    '.asm', '.s', '.nasm', '.gas', '.lst', '.mac',
    '.vh', '.vhd', '.vhdl', '.sv', '.svh', '.svi',
    '.mli', '.fsi', '.fsx', '.fsscript',
    '.scm', '.ss', '.rktl', '.odin', '.d', '.di',
    '.mod.c', '.dts', '.dtsi',
    '.prisma', '.sdl', '.openapi', '.swagger', '.raml', '.wsdl',
    '.wsf',
}

DOCUMENTATION_FILES = {
    '.md', '.markdown', '.txt', '.rtf', '.rst', '.asciidoc', '.adoc',
    '.tex', '.latex', '.wiki', '.org', '.pod', '.rdoc', '.textile',
    '.creole', '.mediawiki', '.dokuwiki', '.asc', '.man', '.mdwn',
    '.doc', '.docx', '.odt', '.pages', '.wpd', '.wps',
    '.pdf', '.xps', '.oxps', '.epub', '.mobi',
    '.ppt', '.pptx', '.key', '.odp', '.pps', '.ppsx',
    '.xls', '.xlsx', '.numbers', '.ods', '.csv', '.tsv',
    '.drawio', '.vsdx', '.vsd', '.dgml', '.dgm',
    'CHANGELOG', 'CONTRIBUTING', 'AUTHORS', 'MAINTAINERS',
    'SECURITY', 'SUPPORT', 'THANKS', 'UPGRADING', 'VERSION',
}

DATA_FILES = {
    '.json', '.xml', '.yaml', '.yml', '.csv', '.tsv', '.xlsx', '.xls',
    '.parquet', '.orc', '.avro', '.arrow', '.feather', '.jsonl', '.ndjson',
    '.db', '.sqlite', '.sqlite3', '.mdb', '.accdb', '.dbf', '.fdb',
    '.rdb', '.pdb', '.odb', '.mdf', '.ldf', '.frm', '.ibd',
    '.hdf', '.h5', '.fits', '.nc', '.cdf', '.grib',
    '.shp', '.shx', '.kml', '.kmz', '.gpx', '.osm', '.geojson',
    '.pb', '.pbtxt', '.ckpt', '.tflite', '.onnx',
    '.pt', '.pth', '.safetensors', '.npy', '.npz',
}

BINARY_FILES = {
    '.exe', '.msi', '.app', '.dmg', '.pkg', '.deb', '.rpm',
    '.apk', '.ipa', '.dll', '.so', '.dylib', '.a', '.lib',
    '.jar', '.war', '.ear', '.aar', '.gem',
    '.whl', '.egg', '.pyd', '.pyo', '.pyc',
    '.obj', '.o', '.ko', '.class', '.dex',
    '.ilk', '.exp', '.pch',
}

MEDIA_FILES = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
    '.webp', '.heic', '.heif', '.raw', '.psd', '.ico', '.icns', '.avif',
    '.svg', '.eps', '.ai',
    '.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.wma', '.opus',
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
    '.fbx', '.3ds', '.blend', '.stl', '.dae', '.dwg', '.dxf',
    '.glb', '.gltf', '.usdz',
}

ARCHIVE_FILES = {
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
    '.tgz', '.tbz2', '.txz', '.cab', '.iso',
    '.bak', '.backup', '.bkp',
}

SYSTEM_FILES = {
    '.tmp', '.temp', '.swp', '.swo', '.swn',
    '.cache', '.part', '.crdownload',
    '.log', '.logs', '.out', '.err',
    '.dump', '.dmp', '.crash', '.core',
    '.lnk', '.url', '.pid', '.lock', '.sock',
}

SECURITY_FILES = {
    '.key', '.pem', '.crt', '.cer', '.der', '.p12', '.pfx',
    '.p7b', '.p7c', '.keystore', '.jks', '.truststore',
    '.csr', '.pub', '.gpg', '.asc',
    '.htpasswd', '.netrc', '.pgpass',
    '.secrets', '.credentials', '.kdbx', '.vault',
}

# Directories always excluded from scanning
EXCLUDED_DIRECTORIES = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv', 'env',
    '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    '.next', '.nuxt', 'dist', 'build', '.build',
    'target', 'out', 'bin', '.gradle', '.mvn',
    '.idea', '.vscode', '.vs',
    'vendor', 'bower_components',
    '.terraform', '.serverless',
    'coverage', '.nyc_output', 'htmlcov',
    '.eggs', '*.egg-info',
}


def get_file_extension(file_path: str) -> str:
    """Extract the file extension from a path (lowercase)."""
    return os.path.splitext(file_path)[1].lower()


def get_file_type(file_path: str) -> str:
    """
    Determine the type of file based on its extension or filename.

    Returns one of: 'Source Code', 'Configuration', 'Documentation',
    'Data File', 'Binary', 'Media', 'Archive', 'System', 'Security', 'Other'.
    """
    filename = os.path.basename(file_path)

    # Check complete filename first (e.g. "Dockerfile", "Makefile")
    if filename in CONFIGURATION_FILES:
        return 'Configuration'
    if filename in DOCUMENTATION_FILES:
        return 'Documentation'

    # Try the last extension (handles compound like .d.ts)
    parts = filename.split('.')
    if len(parts) > 1:
        last_ext = f'.{parts[-1]}'
        for ext_set, label in _EXTENSION_ORDER:
            if last_ext in ext_set:
                return label

    # Fall back to os.path.splitext
    extension = get_file_extension(file_path)
    for ext_set, label in _EXTENSION_ORDER:
        if extension in ext_set:
            return label

    return 'Other'


# Lookup order (source code first to match server behaviour)
_EXTENSION_ORDER = [
    (SOURCE_CODE_FILES, 'Source Code'),
    (CONFIGURATION_FILES, 'Configuration'),
    (DOCUMENTATION_FILES, 'Documentation'),
    (DATA_FILES, 'Data File'),
    (BINARY_FILES, 'Binary'),
    (MEDIA_FILES, 'Media'),
    (ARCHIVE_FILES, 'Archive'),
    (SYSTEM_FILES, 'System'),
    (SECURITY_FILES, 'Security'),
]


def should_exclude_file(file_path: str) -> bool:
    """
    Return True if a file should be excluded from LoC counting.

    Excluded: files inside .git directories, configuration files, and
    non-source categories (docs, data, binary, media, archives, system, security).
    """
    # Anything inside a .git directory
    parts = file_path.replace('\\', '/').split('/')
    if '.git' in parts:
        return True

    filename = os.path.basename(file_path)
    if filename in CONFIGURATION_FILES:
        return True

    extension = get_file_extension(file_path)
    return extension in _EXCLUDED_EXTENSIONS


def should_exclude_directory(dir_name: str) -> bool:
    """Return True if a directory should be skipped entirely during scanning."""
    return dir_name in EXCLUDED_DIRECTORIES


# Pre-computed set of extensions excluded from LoC counting
_EXCLUDED_EXTENSIONS = (
    DOCUMENTATION_FILES | DATA_FILES | BINARY_FILES |
    MEDIA_FILES | ARCHIVE_FILES | SYSTEM_FILES | SECURITY_FILES
)
