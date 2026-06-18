"""Setup configuration for simulator package."""

from setuptools import setup, find_packages
from pathlib import Path

# Read README
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text() if readme_file.exists() else ""

setup(
    name="simulator",
    version="2.0.0",
    description="Microservice Latency Simulator with retry policies, circuit breakers, and fault injection",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Yazhuo Zhang",
    python_requires=">=3.10",
    
    # Package configuration
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    
    # Dependencies
    install_requires=[
        "pydantic>=2.0",
        "pyyaml>=6.0",
        "pandas>=2.0",
        "numpy>=1.24",
        "matplotlib>=3.7",
    ],
    
    # Optional dependencies
    extras_require={
        # Reinforcement-learning extension (optional). Install with:
        #   pip install -e ".[rl]"
        # Version floors match the stack RB-RL.v5 was trained/served with.
        "rl": [
            "stable-baselines3>=2.8.0",
            "gymnasium>=1.2.0",
            "torch>=2.6.0",
            "tensorboard>=2.18.0",
        ],
        "dev": [
            "pytest>=7.0",
            "black>=23.0",
            "mypy>=1.0",
            "flake8>=6.0",
        ],
        "docs": [
            "sphinx>=7.0",
            "sphinx-rtd-theme>=1.3",
        ],
    },
    
    # Entry points for command-line scripts
    entry_points={
        "console_scripts": [
            "ms-sim=simulator.cli:main",
        ],
    },
    
    # Classifiers
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "Topic :: Software Development :: Testing",
        "Topic :: System :: Distributed Computing",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    
    # Include package data
    include_package_data=True,
    zip_safe=False,
)
