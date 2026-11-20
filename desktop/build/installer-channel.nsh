# Installer page: follow Stable (default) or Nightly (issue #218).
#
# Included through nsis.include in electron-builder.yml. The installed app
# reads <userData>\updates\update-channel.json on every update check
# (SourceUpdates.channel() in src/main/source-updates.ts); no file means
# stable. So the installer only ever writes {"channel":"nightly"}, only when
# Nightly was chosen, and only when that file does not exist yet: an upgrade
# or reinstall keeps the choice the install already has, and Stable writes
# nothing.
#
# userData is %APPDATA%\<app name>. Electron takes the name from the packaged
# package.json ("ai-shuffle-desktop"; the stable app never calls app.setName),
# and electron-builder defines that name as APP_PACKAGE_NAME, the same define
# its uninstaller uses to find the app's data.
#
# Silent installs: /CHANNEL=nightly. Without it a silent install, including
# the installer update the app runs itself, writes nothing.

!include LogicLib.nsh
!define SHUFFLE_LOGIN_ITEM_STAMP "resources\login-item-uninstall-v1"
!define SHUFFLE_RUN "Software\Microsoft\Windows\CurrentVersion\Run"
!define SHUFFLE_APPROVAL "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
!define SHUFFLE_LOGIN_ITEM "com.hobbycoders.aishuffle.desktop"

!macro shuffleReadStartupOwner DIRECTORY
  StrCpy $1 ""
  ClearErrors
  FileOpen $0 "${DIRECTORY}\${SHUFFLE_LOGIN_ITEM_STAMP}" r
  ${ifNot} ${Errors}
    FileReadUTF16LE $0 $1
    FileClose $0
  ${endif}
!macroend

!macro shuffleStartupRefuse MESSAGE
  DetailPrint "${MESSAGE}"
  MessageBox MB_OK|MB_ICONSTOP "${MESSAGE}" /SD IDOK
  SetErrorLevel 2
  Quit
!macroend

!ifndef BUILD_UNINSTALLER
  !include nsDialogs.nsh
  !include FileFunc.nsh

  !define SHUFFLE_CHANNEL_DIR "$APPDATA\${APP_PACKAGE_NAME}\updates"
  !define SHUFFLE_CHANNEL_FILE "${SHUFFLE_CHANNEL_DIR}\update-channel.json"

  Var shuffleChannel
  Var shuffleChannelStable
  Var shuffleChannelNightly

  # The app's data is per user even when the app is installed for all users.
  # $installMode is declared by electron-builder's multiUser.nsh, after this
  # file, so these are only used from the macros below.
  !macro shuffleUserContext
    ${if} $installMode == "all"
      SetShellVarContext current
    ${endif}
  !macroend
  !macro shuffleRestoreContext
    ${if} $installMode == "all"
      SetShellVarContext all
    ${endif}
  !macroend

  !macro customHeader
    # The pinned assisted installer inserts this Section before its install
    # Section, after scope/directory selection but BEFORE it runs either old
    # uninstaller. --updated alone cannot tell an upgrade from a move.
    Section "-Check startup registration"
      SectionIn RO
      # installer.nsi selects all-users in its silent machine-upgrade path.
      # Resolve that same destination before comparing it below.
      ${if} ${Silent}
      ${andIf} $hasPerMachineInstallation == "1"
        !insertmacro setInstallModePerAllUsers
      ${endif}

      # Also prevent an explicit destination from overwriting a stamped copy
      # owned by a different account (including when UAC changed accounts).
      ${if} ${FileExists} "$INSTDIR\${SHUFFLE_LOGIN_ITEM_STAMP}"
        !insertmacro shuffleUserContext
        !insertmacro shuffleReadStartupOwner "$INSTDIR"
        StrCpy $2 "$APPDATA"
        !insertmacro shuffleRestoreContext
        ${if} $1 != $2
          !insertmacro shuffleStartupRefuse "This folder contains AI Shuffle installed for another account. Choose a different folder."
        ${endif}
      ${endif}

      ReadRegStr $2 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
      ${if} $2 != ""
      ${andIf} ${FileExists} "$2\${SHUFFLE_LOGIN_ITEM_STAMP}"
        ReadRegStr $3 HKCU "${SHUFFLE_RUN}" "${SHUFFLE_LOGIN_ITEM}"
        ${if} $3 != ""
          ${if} $installMode != "CurrentUser"
          ${orIf} $INSTDIR != $2
            !insertmacro shuffleStartupRefuse "AI Shuffle is registered to start at sign-in. In Settings > General, turn it off or remove the startup item, or uninstall this per-user copy before changing the install scope or folder."
          ${endif}
        ${endif}
      ${endif}
    SectionEnd
  !macroend

  !macro customInit
    StrCpy $shuffleChannel "stable"
    ${GetParameters} $R0
    ClearErrors
    ${GetOptions} $R0 "/CHANNEL=" $R1
    # NSIS compares strings case-insensitively: /CHANNEL=Nightly works too.
    ${ifNot} ${Errors}
    ${andIf} $R1 == "nightly"
      StrCpy $shuffleChannel "nightly"
    ${endif}
  !macroend

  # After the directory page, before the files are installed.
  !macro customPageAfterChangeDir
    Page custom shuffleChannelPageCreate shuffleChannelPageLeave

    Function shuffleChannelPageCreate
      # An installer update, or an install that already has a choice: the
      # page could change nothing, so it is not shown.
      ${if} ${isUpdated}
        Abort
      ${endif}
      !insertmacro shuffleUserContext
      ${if} ${FileExists} "${SHUFFLE_CHANNEL_FILE}"
        !insertmacro shuffleRestoreContext
        Abort
      ${endif}
      !insertmacro shuffleRestoreContext

      !insertmacro MUI_HEADER_TEXT "Update channel" "Choose which updates this install receives."
      nsDialogs::Create 1018
      Pop $0
      ${if} $0 == error
        Abort
      ${endif}
      ${NSD_CreateRadioButton} 0 0 100% 12u "Stable (recommended): tested releases"
      Pop $shuffleChannelStable
      ${NSD_AddStyle} $shuffleChannelStable ${WS_GROUP}
      ${NSD_CreateRadioButton} 0 18u 100% 12u "Nightly: the newest changes every day; may break"
      Pop $shuffleChannelNightly
      ${NSD_CreateLabel} 0 42u 100% 30u "You can leave Nightly again in Settings > System > Updates once a stable release catches up with your version."
      Pop $0
      ${if} $shuffleChannel == "nightly"
        ${NSD_Check} $shuffleChannelNightly
      ${else}
        ${NSD_Check} $shuffleChannelStable
      ${endif}
      nsDialogs::Show
    FunctionEnd

    Function shuffleChannelPageLeave
      ${NSD_GetState} $shuffleChannelNightly $0
      ${if} $0 == ${BST_CHECKED}
        StrCpy $shuffleChannel "nightly"
      ${else}
        StrCpy $shuffleChannel "stable"
      ${endif}
    FunctionEnd
  !macroend

  !macro customInstall
    ${if} $shuffleChannel == "nightly"
      Push $0
      !insertmacro shuffleUserContext
      # Never overwrite an existing choice. Rename also refuses to replace a
      # file that appeared meanwhile; the temporary file is then removed.
      ${ifNot} ${FileExists} "${SHUFFLE_CHANNEL_FILE}"
        CreateDirectory "${SHUFFLE_CHANNEL_DIR}"
        ClearErrors
        FileOpen $0 "${SHUFFLE_CHANNEL_FILE}.tmp" w
        ${ifNot} ${Errors}
          FileWrite $0 '{"channel":"nightly"}'
          FileClose $0
          Rename "${SHUFFLE_CHANNEL_FILE}.tmp" "${SHUFFLE_CHANNEL_FILE}"
          Delete "${SHUFFLE_CHANNEL_FILE}.tmp"
        ${endif}
      ${endif}
      !insertmacro shuffleRestoreContext
      Pop $0
    ${endif}

    # A source update does not replace the uninstaller. Stamp only copies
    # installed for this user, whose HKCU entries this uninstaller can clean.
    ${if} $installMode == "CurrentUser"
      Push $0
      ClearErrors
      FileOpen $0 "$INSTDIR\${SHUFFLE_LOGIN_ITEM_STAMP}" w
      ${ifNot} ${Errors}
        FileWriteUTF16LE $0 "$APPDATA"
        FileClose $0
      ${endif}
      Pop $0
    ${endif}
  !macroend
!else
  !macro customUnInstall
    ${if} ${FileExists} "$INSTDIR\${SHUFFLE_LOGIN_ITEM_STAMP}"
      !insertmacro shuffleReadStartupOwner "$INSTDIR"
      ${if} $installMode != "CurrentUser"
      ${orIf} $1 != "$APPDATA"
        !insertmacro shuffleStartupRefuse "Uninstall this per-user copy of AI Shuffle from the Windows account that installed it."
      ${endif}
      # New installers allow --updated to replace this copy only in place
      # while startup is registered. Preserve the entry and Windows approval.
      ${ifNot} ${isUpdated}
        ReadRegStr $3 HKCU "${SHUFFLE_RUN}" "${SHUFFLE_LOGIN_ITEM}"
        ${if} $3 == ""
        ${orIf} $3 == '"$INSTDIR\${APP_EXECUTABLE_FILENAME}"'
          DeleteRegValue HKCU "${SHUFFLE_RUN}" "${SHUFFLE_LOGIN_ITEM}"
          DeleteRegValue HKCU "${SHUFFLE_APPROVAL}" "${SHUFFLE_LOGIN_ITEM}"
        ${endif}
      ${endif}
    ${endif}
  !macroend
!endif
