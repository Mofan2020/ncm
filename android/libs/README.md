pywebview-android.jar
======================

The Java classes pywebview's Android backend needs.

    com/pywebview/PyWebViewClient
    com/pywebview/PyWebChromeClient
    com/pywebview/PyJavascriptInterface
    com/pywebview/EventCallbackWrapper
    com/pywebview/JsApiCallbackWrapper
    com/pywebview/JsApiCallbackWrapper
    com/pywebview/JavascriptValueCallback
    com/pywebview/WebViewRequestInterceptor

They are not in the PyPI package: ``webview/platforms/android/jclass/`` names
them, but the classes themselves only exist as compiled Java.  Without them
the app dies on launch with a ClassNotFoundError the moment the WebView
client is created.

Where it comes from
-------------------

The upstream repository, at ``webview/lib/pywebview-android.jar``::

    gh api repos/r0x0r/pywebview/contents/webview/lib/pywebview-android.jar \\
        -q '.content' | base64 -d > android/libs/pywebview-android.jar

It is built there from the sources in ``interop/android/lib/src/main/java``,
which are in the same repository.

    size   14789 bytes
    sha256 d5715b9c07ff2858c854bda872191c581dbbe101acabeff0076fc69edfc612a8
    git blob sha (upstream) 48996844085e88fece839e6d97e4bbc857de0401

The build checks the jar is there and holds the six classes the Python side
looks up, so a missing or truncated jar fails the build instead of shipping
an app that crashes on the phone.
