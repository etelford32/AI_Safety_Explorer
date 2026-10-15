# Signing and notarizing the app

The macOS app is built by CI (`.github/workflows/app.yml`) and attached to every release as
`AI-Safety-Explorer-macOS.dmg`. How it opens the first time depends on how it is signed:

| Signed with | First launch |
|---|---|
| **Ad hoc** (the default: no Apple account) | macOS blocks it until the user approves it once, under **System Settings → Privacy & Security → Open Anyway** ([INSTALL.md](INSTALL.md#the-first-launch)). |
| **A Developer ID, and notarized** | It opens like any app from the web, with the usual one-line *"downloaded from the internet"* confirmation. |

Everything for the second row is already in the workflow. It turns on when the repository has
the secrets below, and until then the build skips those steps.

## What you need, once

1. **Membership in the Apple Developer Program.** It costs US$99 a year and an individual
   membership is enough ([developer.apple.com/programs](https://developer.apple.com/programs/)).
   Enrolment can take a day or two.
2. **A "Developer ID Application" certificate.** On a Mac, open Xcode → *Settings* → *Accounts*,
   select your team, choose *Manage Certificates…*, then **+** → *Developer ID Application*.
   Then export it:
   - In Keychain Access, open *My Certificates*, right-click the certificate and choose
     *Export…*.
   - Save it as a `.p12` file with a password.
   - Run `base64 -i DeveloperID.p12 | pbcopy` to copy it for the secret below.
3. **An App Store Connect API key, for notarization.** In App Store Connect, go to
   *Users and Access* → *Integrations* → *App Store Connect API* → *Team Keys*, and choose
   **+** with *Developer* access. Download the `AuthKey_XXXXXXXXXX.p8` file (Apple allows this
   only once). Note the *Key ID* and the *Issuer ID* shown on that page.

## The repository secrets

Add these under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `MACOS_CERTIFICATE` | the base64 of the `.p12` |
| `MACOS_CERTIFICATE_PASSWORD` | the `.p12`'s password |
| `APPLE_API_KEY` | the contents of the `.p8` file (paste the text as it is) |
| `APPLE_API_KEY_ID` | the Key ID |
| `APPLE_API_ISSUER` | the Issuer ID |
| `MACOS_SIGNING_IDENTITY` *(optional)* | e.g. `Developer ID Application: Elliot Telford (TEAMID)`; the default is the certificate's own |

Then run the build once by hand: **Actions → macOS app → Run workflow**. The step *What
Gatekeeper will say* should report that the app is notarized. From then on every release is
signed, notarized and stapled.

These steps follow Apple's documented process, but they have not run against a real
certificate yet. Treat that first run as the test. If it fails, the job log says which step
failed, and notarization failures include Apple's report.

## What the workflow does with them

1. **`packaging/sign.sh`** imports the certificate into a temporary keychain. It then signs
   every binary in the app, inside out: extension modules, libraries, the Python framework,
   the executables and finally the app. Each is signed with the hardened runtime and a secure
   timestamp. The executables also get `packaging/entitlements.plist`, which contains the two
   exceptions an embedded Python needs.
2. **`packaging/notarize.sh`** submits the app to Apple's notary service, waits for the
   verdict, and staples the ticket to the app. Stapling lets a Mac with no network still
   verify it.
3. **`packaging/make_dmg.sh`** builds the disk image from the stapled app and signs it. It
   then opens the image read-only and runs the app from it, as a downloader would.
4. The disk image is notarized and stapled too, and both files are attached to the release.

The self-updating app keeps its signature through updates. Updates download Explorer code
into the data folder, `~/Library/Application Support/AI Safety Explorer/`, and never modify
the signed app. So the app needs signing again only when the app itself is rebuilt, which
happens with each release.

Secrets are never given to workflows run for pull requests from forks. Those builds stay ad
hoc.
