from setuptools import setup, find_packages

setup(
    name="trading-common",
    version="1.0.0",
    description="Shared utilities for AI Trading Bot",
    packages=find_packages(),
    install_requires=[
        "redis>=7.1.0",
        "psycopg[binary]>=3.3.2",
        "python-dotenv>=1.2.1",
        "pandas>=2.2.0",
        "pandas-ta>=0.4.67b0",
        "numpy>=1.26.0",
    ],
    python_requires=">=3.11",
)
