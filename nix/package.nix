{ lib
, python312Packages
, swiProlog
, makeWrapper
}:

python312Packages.buildPythonApplication {
  pname = "llm-log";
  version = "0.1.0";
  src = ../.;
  pyproject = true;

  build-system = [ python312Packages.setuptools ];
  dependencies = [ python312Packages.aiohttp ];
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ swiProlog ];

  checkPhase = ''
    runHook preCheck
    ${python312Packages.python.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  postInstall = ''
    wrapProgram "$out/bin/llm-log" \
      --prefix PATH : ${lib.makeBinPath [ swiProlog ]}
  '';
}
