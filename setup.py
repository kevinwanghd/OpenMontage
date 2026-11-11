# risk:untested reason:"打包元数据无运行逻辑, console_scripts 入口由 tests/contracts/test_shortdrama_cli.py 断言, thin 模式无法绑定运行证据" owner:@kevinwanghd reviewed:2026-11-11
from setuptools import setup, find_packages

setup(
    name="openmontage",
    version="0.1.0",
    description="AI-Orchestrated Video Production Platform",
    packages=find_packages(),
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "om = shortdrama.cli:main",
        ],
    },
    install_requires=[
        "pyyaml>=6.0",
        "pydantic>=2.0",
        "jsonschema>=4.20",
        "python-dotenv>=1.0",
        "Pillow>=10.0",
        "requests>=2.31",
        "google-genai>=1.0.0",
        "openai>=2.44.0",
    ],
)
