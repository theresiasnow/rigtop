; Inno Setup script for rigtop
; Build: iscc /DAppVersion=v0.6.1 rigtop.iss

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppName=rigtop
AppVersion={#AppVersion}
AppPublisher=theresiasnow
AppPublisherURL=https://github.com/theresiasnow/rigtop
AppSupportURL=https://github.com/theresiasnow/rigtop/issues
DefaultDirName={autopf}\rigtop
DefaultGroupName=rigtop
OutputDir=installer
OutputBaseFilename=rigtop-{#AppVersion}-setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\rigtop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\rigtop"; Filename: "{app}\rigtop.exe"; \
  Comment: "Ham radio rig dashboard"
Name: "{group}\Uninstall rigtop"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\rigtop.exe"; Parameters: "--version"; \
  Description: "Verify installation"; Flags: nowait postinstall skipifsilent

