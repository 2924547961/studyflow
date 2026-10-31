# Email integration reference

SMTP provider selection and attachment workflow were informed by TimeCyber/email-mcp,
https://github.com/TimeCyber/email-mcp, commit e41e149936fda3f3f5119880842e698d1dc3ea71.
StudyFlow uses a separate Python stdlib implementation with per-learning-account
credentials rather than that project's global environment configuration.

MIT License

Copyright (c) 2025 TimeCyber

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


# Desktop and portable runtime

StudyFlow is distributed under GPL-3.0-only; see LICENSE. The usage statement describes the intended use and does not add restrictions to the GPL. The corresponding StudyFlow source and build scripts accompany the same release.

PyQt6 is GPLv3/commercial; this package uses the GPLv3 distribution. Qt libraries retain their upstream LGPL/GPL notices. See https://en.riverbankcomputing.com/software/pyqt and the bundled licenses directory.

Dependency versions and upstream project/source locations are listed in licenses/dependencies.json in the portable package. Python, Playwright/Node.js, OpenSSL and all other dependencies retain their own licenses.
