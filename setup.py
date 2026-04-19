from setuptools import setup, find_packages

setup(
    name="stave-synth",
    version="0.1.0",
    description="Live MIDI synthesizer for Raspberry Pi 5 with touchscreen control",
    author="Cody Van Scyoc",
    packages=find_packages(),
    include_package_data=True,
    package_data={"": ["ui/*"]},
    install_requires=[
        # python-jack-client is broken on aarch64 — the C bridge (jack_bridge.so)
        # replaces it, so don't drag the CFFI dep along.
        "numpy>=1.24.0",
        "scipy>=1.11.0",
        "websockets>=11.0",
        "pyfluidsynth>=1.3.0",
        "psutil>=5.9.0",
        "cffi>=1.16",
    ],
    extras_require={
        # Native fullscreen window via WebKit. Skip on headless installs —
        # the browser UI at http://<host>:8080 works without it.
        "gui": ["pywebview>=4.0"],
    },
    entry_points={
        "console_scripts": [
            "stave-synth=stave_synth.main:main",
        ],
    },
    python_requires=">=3.11",
)
