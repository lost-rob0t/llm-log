{ pkgs }:

let
  cl = pkgs.sbcl.pkgs;
  source = pkgs.fetchFromGitHub {
    owner = "mdbergmann";
    repo = "cl-gserver";
    rev = "013ab6370042686e65943568b0d97e33319c0f54";
    hash = "sha256-z+AKk8Y09rpF+NgyKhcHNvn0jsjeGe3dSibkv8yySKg=";
  };
in
pkgs.sbcl.buildASDFSystem {
  pname = "sento";
  version = "3.4.4";
  src = source;
  systems = [ "sento" ];
  lispLibs = with cl; [
    alexandria
    log4cl
    bordeaux-threads
    cl-speedy-queue
    str
    binding-arrows
    timer-wheel
    local-time-duration
    atomics
  ];
}
