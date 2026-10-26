.class public final Lua/bandera/bridge/Bridge;
.super Ljava/lang/Object;
.source "Bridge.smali"

# Lets the Bandera app accept the ESP32-C5 Arinst analyzer. The C5 speaks the Arinst scn20 protocol
# natively, so no translation is needed; the only obstacle is its fixed USB id (Espressif
# VID 0x303a / PID 0x1001, from its USB-Serial/JTAG controller, which cannot be changed). This
# reports the C5 to the app as an "Arinst SSA-Universal" so the analyzer-selection code accepts it;
# every other device keeps its real manufacturer name. Called from the patched smali in i9/j and
# ConnectionService in place of UsbDevice.getManufacturerName().

# Replaces UsbDevice.getManufacturerName() at the app's analyzer-selection sites.
.method public static manufacturer(Landroid/hardware/usb/UsbDevice;)Ljava/lang/String;
    .locals 2
    .param p0, "d"    # Landroid/hardware/usb/UsbDevice;

    const/4 v0, 0x0

    if-nez p0, :cond_have_device

    return-object v0

    :cond_have_device
    # VID 0x303a && PID 0x1001 -> the ESP32-C5 Arinst firmware
    invoke-virtual {p0}, Landroid/hardware/usb/UsbDevice;->getVendorId()I

    move-result v1

    const/16 v0, 0x303a

    if-ne v1, v0, :cond_real

    invoke-virtual {p0}, Landroid/hardware/usb/UsbDevice;->getProductId()I

    move-result v1

    const/16 v0, 0x1001

    if-ne v1, v0, :cond_real

    const-string v0, "Arinst SSA-Universal"

    return-object v0

    :cond_real
    invoke-virtual {p0}, Landroid/hardware/usb/UsbDevice;->getManufacturerName()Ljava/lang/String;

    move-result-object v0

    return-object v0
.end method
