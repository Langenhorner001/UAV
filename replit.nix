{pkgs}: {
  deps = [
    pkgs.xvfb-run
    pkgs.tor
    pkgs.geckodriver
    pkgs.chromedriver
    pkgs.chromium
  ];
}
