// Runs under Windows Script Host via install-windows-shortcuts.cmd.
var files = new ActiveXObject("Scripting.FileSystemObject");
var shell = new ActiveXObject("WScript.Shell");
var root = files.GetParentFolderName(WScript.ScriptFullName);
var launcher = files.BuildPath(root, "start-windows.cmd");
var icon = files.BuildPath(root, "assets\\ScreenDesk.ico");
var description = "ScreenDesk SSH workspace";

function install() {
    if (!files.FileExists(launcher) || !files.FileExists(icon)) {
        throw new Error("Keep the installer in the complete ScreenDesk project folder.");
    }

    var folders = ["Desktop", "Programs"];
    var shortcuts = [];
    for (var i = 0; i < folders.length; i++) {
        var folder = shell.SpecialFolders.Item(folders[i]);
        if (!folder || !files.FolderExists(folder)) {
            throw new Error("Windows could not locate your " + folders[i] + " folder.");
        }
        var path = files.BuildPath(folder, "ScreenDesk.lnk");
        var shortcut = shell.CreateShortcut(path);
        if (files.FileExists(path) && shortcut.Description !== description) {
            throw new Error("A different shortcut already exists at " + path + ".");
        }
        shortcuts.push(shortcut);
    }

    for (var j = 0; j < shortcuts.length; j++) {
        var link = shortcuts[j];
        link.TargetPath = launcher;
        link.WorkingDirectory = root;
        link.IconLocation = icon + ",0";
        link.Description = description;
        link.Save();
    }
    WScript.Echo("ScreenDesk is ready on your Desktop and in the Start menu.");
}

try {
    install();
} catch (error) {
    WScript.StdErr.WriteLine(error.message);
    WScript.Quit(1);
}
