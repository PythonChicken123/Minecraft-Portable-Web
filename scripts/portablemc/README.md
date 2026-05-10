# Portable Minecraft Launcher
Cross platform command line utility for launching Minecraft quickly and reliably with 
included support for Mojang versions and popular mod loaders. It is also available as 
a Rust crate for developers ~~and bindings for C and Python~~ (yet to come).

## Table of contents
- [Features](#features)
- [Installation](#installation)
  - [Binaries](#binaries)
  - [Cargo](#cargo)
  - [Linux third-party packages](#linux-third-party-packages)
    - [Arch Linux](#arch-linux)
- [Contribute](#contribute)
  - [Repositories](#repositories)
  - [Contributors](#contributors)
  - [Sponsors](#sponsors)
- [Rust documentation ⇗](https://docs.rs/portablemc/latest/portablemc)

## Features

- **Install and launch a version in 1 command.**
- Supports Mojang versions, and many mod loaders which are installed seamlessly: **Forge**, **NeoForge**, **Fabric**, Quilt, LegacyFabric and Babric.
- Automatically fixing known issues of the game, can be disabled if desired;
- Options for supporting unsupported systems and architectures, such as RaspberryPi/Arm;
- And more flags and options to configure the launch...
- Able to launch **offline** or with a **Microsoft account**.
- Browse the supported versions, for any kind of versions.
- Very descriptive output and errors, with configurable verbosity.
- Various output mode, including machine-readable output mode.
- Fast parallel downloading of game files.
- Find your system Java runtime, if compatible, and fallback to Mojang provided ones when available.
- Distributed for many mainstream OS and architectures.

## Installation

### Cargo

![Crates.io Version](https://img.shields.io/crates/v/portablemc-cli)

If you have a Rust toolchain with Cargo, you can build and install PortableMC and its 
CLI straight from [crates.io](https://crates.io/crates/portablemc-cli), this is where 
the latest development versions are pushed first, before being built for specific 
targets.

```sh
cargo install portablemc-cli
```

If you are a developer willing to use PortableMC as a library to develop your own 
launcher, it is also available on [crates.io](https://crates.io/crates/portablemc).

```sh
cargo add portablemc
```

#### Arch Linux

![AUR Version](https://img.shields.io/aur/version/portablemc)

Arch Linux packages are maintained by PortableMC team.

- Build from source: [`portablemc`](https://aur.archlinux.org/packages/portablemc), available on AUR
- Prebuilt binaries: [`portablemc-bin`](https://aur.archlinux.org/packages/portablemc-bin), available on AUR

Prebuilt binaries requires you to install the PGP certificate, as described [above](#binaries)

### Releasing

Releasing process is mostly managed by GitHub actions, it reacts to new tags being 
pushed to the repository by a repository admin. This tag should be named `v<version>` where
`<version>` is the same as the one in `Cargo.toml`, if not matching, the actions will fail.

Once completed, a draft release note is attached to that new tag under [releases](https://github.com/theorzr/portablemc/releases),
you should complete it with the actual changelog, and then publish the release note.
Note that the release artifacts are automatically uploaded and signed by the actions.

Then, you should manage the releasing of official third-party packaging, such as 
[portablemc-arch](https://codeberg.org/portablemc/portablemc-arch) and
[portablemc-bin-arch](https://codeberg.org/portablemc/portablemc-bin-arch).