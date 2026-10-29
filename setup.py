from setuptools import setup, find_packages

setup(
    name="proofpush",
    version="0.1.0",
    description="ProofPush: Local AI Website Regression Auditor",
    packages=find_packages(),
    install_requires=[
        "click>=8.0",
        "rich>=12.0",
        "pyyaml>=6.0",
        "playwright>=1.30",
        "pillow>=9.0",
    ],
    entry_points={
        "console_scripts": [
            "proofpush=change_auditor.cli:main",
            "change-auditor=change_auditor.cli:main",
        ],
    },
)
