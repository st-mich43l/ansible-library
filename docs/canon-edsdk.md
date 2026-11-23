# Provision the Canon SDK on Windows

This playbook copies the separately vaulted Canon EDSDK 13.21.10 x64 DLLs from the control node to `sdk\canon` next to `ApexVoidPhotobooth.exe`. The application discovers that directory at startup; no password or download service is embedded in the application. Restart the app after provisioning. This does not install the application or verify that an EOS R50 works.

The Canon licence must permit the intended storage, access and use; encryption restricts access but does not itself grant redistribution rights. Restrict repository, vault-password and target access to authorised operators. The decrypted DLLs are present on the target and may be present in Ansible controller/Windows temporary files during transfer. Protect those machines and use encrypted WinRM/SSH transport. Do not commit plaintext SDK files or passwords.

Install the collections in `requirements.yml`, configure a Windows host with Ansible over WinRM or SSH, and create a separate inventory, for example:

```yaml
all:
  children:
    photobooth_windows:
      hosts:
        booth01:
          ansible_host: booth01.example.local
          ansible_connection: winrm
          ansible_user: YOUR_WINDOWS_USER
          canon_photobooth_dir: 'C:\Apps\ApexVoidPhotobooth'
```

Use your normal secure Ansible credential mechanism and verified encrypted transport. Close the application before replacing its SDK files. Then run:

```sh
ansible-playbook -i /secure/path/windows-inventory.yml playbooks/canon-edsdk.yml --ask-vault-pass
```

The role requires an existing application executable, copies the two x64 files into its searched directory, and verifies their SHA-256 checksums. Existing different files at that path are replaced. `--check` does not verify the eventual installed hashes. If the camera still cannot connect, use the application's Canon diagnostics and hardware checklist; a copied DLL does not prove a working camera session.

## Preparing or updating vaulted files

Place only encrypted files at `roles/canon_edsdk/files/EDSDK.dll.vault` and `EdsImage.dll.vault`. Use `ansible-vault encrypt --output` on each x64 DLL, with the same securely managed vault password used by the deployment, and update the expected SHA-256 hashes in the role when changing SDK version. Confirm both files begin with the Ansible Vault header and decrypt to the expected hashes before committing. Never put the password or plaintext DLLs into Git. For the current package, the input DLLs are in `Windows/EDSDK_64/Dll`.
