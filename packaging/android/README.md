# Miracii Android v0.0.1（测试）

这是连电脑的 WebView 客户端，不在手机里跑模型。**不要用 Visual Studio 的 Android / Xamarin / MAUI 工程。** 本目录是 Gradle + Kotlin，用 **Android Studio**。

## 打 APK 需要的环境

1. [Android Studio](https://developer.android.com/studio)（自带 JDK 17 和 SDK Manager）
2. 第一次打开本目录时，让 Studio 下载：
   - Android SDK Platform 34
   - Build-Tools
   - 一个系统镜像（若要用模拟器）
3. 接受许可证。Studio 会在本目录生成 Gradle Wrapper（`gradlew.bat`）
4. 真机：开「开发者选项 → USB 调试」

不需要：Visual Studio、.NET、Xamarin、Unity。仓库里的 Python 只跑在电脑上。

本机已有 OpenJDK 21 也可以，但让 Android Studio 用它自带的 JBR 更省事。

## 电脑侧

同一局域网：

```text
python -m app --lan --web
```

记下 `http://<局域网IP>:7788`。Windows 防火墙若拦住 7788，放行一次。

## 安装 / 出包

1. Android Studio → File → Open → `packaging/android`
2. 等 Gradle 同步完
3. 调试：连手机或模拟器，Run（模拟器默认 `http://10.0.2.2:7788`）
4. 调试 APK：Build → Build Bundle(s) / APK(s) → Build APK(s)  
   或同步完成后在该目录执行 `gradlew.bat assembleDebug`  
   产物：`app/build/outputs/apk/debug/app-debug.apk`
5. 右上角「服务器」填电脑打印的地址
6. 发图：页面上的「图」会调系统相册（WebView `onShowFileChooser`）

没有 SDK 时，用手机浏览器打开同一个 URL 即可。

versionName `0.0.1`。协议和封装都会再改。
