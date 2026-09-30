#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NetEase Music Playlist Downloader - GUI Application
Entry point for the pywebview-based desktop application.
"""
import os
import sys
import webview
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from src.gui.bridge import GuiBridge
from src.config import get_settings


def main():
    """Main entry point for GUI application."""
    settings = get_settings()
    
    # Get HTML file path
    html_path = Path(__file__).parent / 'src' / 'gui' / 'assets' / 'index.html'
    if not html_path.exists():
        print(f"Error: HTML file not found at {html_path}")
        sys.exit(1)
    
    # Create bridge
    bridge = GuiBridge()
    
    # Create window
    window = webview.create_window(
        title='网易云音乐歌单下载器',
        url=str(html_path),
        js_api=bridge,
        width=settings.ui.window_width,
        height=settings.ui.window_height,
        min_size=(800, 600),
        text_select=True,
    )
    
    # Set window reference in bridge
    bridge.set_window(window)
    
    # Start webview
    webview.start(debug=settings.debug, gui='cef' if sys.platform == 'win32' else None)


if __name__ == '__main__':
    main()