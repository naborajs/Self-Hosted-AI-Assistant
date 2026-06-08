from setuptools import find_packages, setup

setup(
    name="local_ai_assistant",
    version="0.1.0",
    description="Self-hosted WhatsApp-first AI assistant powered by Ollama.",
    author="Local AI Team",
    python_requires=">=3.12",
    packages=find_packages(include=["ai", "whatsapp", "telegram", "memory", "database", "tools", "api", "tests"]),
    install_requires=[
        "fastapi>=0.110.0",
        "uvicorn[standard]>=0.23.0",
        "pydantic>=2.8",
        "python-dotenv>=1.0",
        "httpx>=0.26",
        "SQLAlchemy>=2.0",
        "aiosqlite>=0.20",
        "python-multipart>=0.0.6",
        "python-telegram-bot>=20.0",
        "cryptography>=41.0",
        "passlib[bcrypt]>=1.7",
        "python-jose[cryptography]>=3.1",
        "aiofiles>=23.0",
        "Jinja2>=3.1",
    ],
    classifiers=[
        "Programming Language :: Python :: 3.12",
        "Framework :: FastAPI",
        "Operating System :: OS Independent",
    ],
    include_package_data=True,
)
