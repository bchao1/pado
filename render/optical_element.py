import numpy as np
import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt


class OpticalElement:
    def __init__(self, R, C, pitch, wvl, device, name="not defined",B=1):

        self.name = name
        self.B = B
        self.R = R
        self.C = C
        self.pitch = pitch
        self.device = device

        self.amplitude_change = torch.ones((B, 1, R, C), device=self.device)
        self.phase_change = torch.zeros((B, 1, R, C), device=self.device)
        self.wvl = wvl

    def shape(self):
        return (self.B,1,self.R,self.C)

    def set_pitch(self, pitch):
        self.pitch = pitch

    def resize(self, target_pitch, interp_mode='bilinear'):
        scale_factor = self.pitch / target_pitch
        self.amplitude_change = F.interpolate(self.amplitude_change, scale_factor=scale_factor,
                                              mode=interp_mode)
        self.phase_change = F.interpolate(self.phase_change, scale_factor=scale_factor,
                                          mode=interp_mode)
        self.set_pitch(target_pitch)
        self.R = self.amplitude_change.shape[-2]
        self.C = self.amplitude_change.shape[-1]

    def get_amplitude_change(self):
        return self.amplitude_change

    def get_phase_change(self):
        return self.phase_change

    def set_amplitude_change(self, amplitude):
        assert amplitude.shape[2] == self.R and amplitude.shape[3] == self.C
        self.amplitude_change = amplitude

    def set_phase_change(self, phase):
        assert phase.shape[2] == self.R and phase.shape[3] == self.C
        self.phase_change = phase

    def pad(self, pad_width, padval=0):
        if padval == 0:
            self.amplitude_change = torch.nn.functional.pad(self.get_amplitude_change(), pad_width)
            self.phase_change = torch.nn.functional.pad(self.get_phase_change(), pad_width)
        else:
            return NotImplementedError('only zero padding supported')

        self.R += pad_width[0] + pad_width[1]
        self.C += pad_width[2] + pad_width[3]

    def forward(self, light, interp_mode='bilinear'):
        if light.pitch > self.pitch:
            light.resize(self.pitch, interp_mode)
            light.set_pitch(self.pitch)
        elif light.pitch < self.pitch:
            self.resize(light.pitch, interp_mode)
            self.set_pitch(light.pitch)

        if light.wvl != self.wvl:
            return NotImplementedError('wavelength should be same for light and optical elements')

        r1 = np.abs((light.R - self.R)//2)
        r2 = np.abs(light.R - self.R) - r1
        pad_width = (r1, r2, 0, 0)
        if light.R > self.R:
            self.pad(pad_width)
        elif light.R < self.R:
            light.pad(pad_width)

        c1 = np.abs((light.C - self.C)//2)
        c2 = np.abs(light.C - self.C) - c1
        pad_width = (0, 0, c1, c2)
        if light.C > self.C:
            self.pad(pad_width)
        elif light.C < self.C:
            light.pad(pad_width)

        light.set_phase(light.get_phase() + self.get_phase_change())
        light.set_amplitude(light.get_amplitude() * self.get_amplitude_change())

        return light

    def visualize(self, b=0):
        plt.figure(figsize=(13,6))

        plt.subplot(121)
        plt.imshow(self.get_amplitude_change().data.cpu()[b,...].squeeze())
        plt.title('amplitude')
        plt.colorbar()

        plt.subplot(122)
        plt.imshow(self.get_phase_change().data.cpu()[b,...].squeeze())
        plt.title('phase')
        plt.colorbar()

        plt.suptitle('%s, (%d,%d), pitch:%.2f[um], wvl:%.2f[nm], device:%s'
                     %(self.name, self.R, self.C, self.pitch/1e-6, self.wvl/1e-9, self.device))
        plt.show()


class RefractiveLens(OpticalElement):
    def __init__(self, R, C, pitch, focal_length, wvl, device):
        super().__init__(R, C, pitch, wvl, device, name="refractive_lens")

        self.set_focal_length(focal_length)
        self.set_phase_change( self.compute_phase(self.wvl, shift_x=0, shift_y=0) )

    def set_focal_length(self, focal_length):
        self.focal_length = focal_length

    def compute_phase(self, wvl, shift_x=0, shift_y=0):
        bw_R = self.R*self.pitch
        bw_C = self.C*self.pitch

        x = np.arange(-bw_C/2, bw_C/2, self.pitch)
        x = x[:self.R]
        y = np.arange(-bw_R/2, bw_R/2, self.pitch)
        y = y[:self.C]
        xx,yy = np.meshgrid(x,y)

        theta_change = torch.tensor((-2*np.pi / wvl)*((xx-shift_x)**2 + (yy-shift_y)**2), device=self.device) / (2*self.focal_length)
        theta_change = torch.unsqueeze(torch.unsqueeze(theta_change, axis=0), axis=0)
        theta_change %= 2*np.pi
        theta_change -= np.pi
        
        return theta_change

def height2phase(height, wvl, RI, wrap=True):
    dRI = RI - 1
    wv_n = 2. * np.pi / wvl
    phi = wv_n * dRI * height
    if wrap:
        phi %= 2 * np.pi
    return phi

def phase2height(phase, wvl, RI):
    dRI = RI - 1
    return wvl * phase / (2 * np.pi) / dRI

def radius2phase(r, f, wvl):
    return (2 * np.pi * (np.sqrt(r * r + f * f) - f) / wvl) % (2 * np.pi)

class DOE(OpticalElement):
    '''
    DOE with complex wavefront modulation
    '''
    def __init__(self, R, C, pitch, material, wvl, device, height=None, phase=None, amplitude=None):

        super().__init__(R, C, pitch, wvl, device, name="doe")

        self.material = material
        self.height = None

        if amplitude is None:
            amplitude = torch.ones((1, 1, self.R, self.C), device=self.device)

        if height is None and phase is not None:
            self.mode = 'phase'
            self.set_phase_change(phase, wvl)
            self.set_amplitude_change(amplitude)
        elif height is not None and phase is None:
            self.mode = 'height'
            self.set_height(height)
            self.set_amplitude_change(amplitude)
        elif (height is None) and (phase is None) and (amplitude is None):
            self.mode = 'phase'
            phase = torch.zeros((1, 1, self.R, self.C), device=self.device)
            self.set_amplitude_change(amplitude)
            self.set_phase_change(phase, wvl)


    def change_wvl(self, wvl):
        height = self.get_height()
        self.wvl = wvl
        phase = height2phase(height, self.wvl, self.material.get_RI(self.wvl))
        self.set_phase_change(phase, self.wvl)

    def set_diffraction_grating_1d(self, slit_width, minh, maxh):
        slit_width_px = np.round(slit_width / self.pitch)
        slit_space_px = slit_width_px

        dg = np.zeros((self.R, self.C))
        slit_num_r = self.R // (2 * slit_width_px)
        slit_num_c = self.C // (2 * slit_width_px)

        dg[:] = minh

        for i in range(int(slit_num_c)):
            minc = int((slit_width_px + slit_space_px) * i)
            maxc = int(minc + slit_width_px)

            dg[:, minc:maxc] = maxh
        pc = torch.tensor(dg.astype(np.float32), device=self.device).unsqueeze(0).unsqueeze(0)
        self.set_phase_change(pc, self.wvl)

    def set_diffraction_grating_2d(self, slit_width, minh, maxh):
        slit_width_px = np.round(slit_width / self.pitch)
        slit_space_px = slit_width_px

        dg = np.zeros((self.R, self.C))
        slit_num_r = self.R // (2 * slit_width_px)
        slit_num_c = self.C // (2 * slit_width_px)

        dg[:] = minh

        for i in range(int(slit_num_r)):
            for j in range(int(slit_num_c)):
                minc = int((slit_width_px + slit_space_px) * j)
                maxc = int(minc + slit_width_px)
                minr = int((slit_width_px + slit_space_px) * i)
                maxr = int(minr + slit_width_px)

                dg[minr:maxr, minc:maxc] = maxh

        pc = torch.tensor(dg.astype(np.float32), device=self.device).unsqueeze(0).unsqueeze(0)
        self.set_phase_change(pc, self.wvl)

    def set_Fresnel_lens(self, focal_length, shift_x=0, shift_y=0):
        '''
        focal_length: in meter
        shift_x: in meter
        shift_y: in meter
        '''

        x = np.arange(-self.C*self.pitch/2, self.C*self.pitch/2, self.pitch)
        y = np.arange(-self.R*self.pitch/2, self.R*self.pitch/2, self.pitch)
        xx,yy = np.meshgrid(x,y)
        xx = torch.tensor(xx, device=self.device)
        yy = torch.tensor(yy, device=self.device)

        phase = (-2*np.pi / self.wvl) * (torch.sqrt((xx-shift_x)**2 + (yy-shift_y)**2 + focal_length**2) - focal_length)
        phase = phase % (2*np.pi)
        phase -= np.pi
        phase = phase.unsqueeze(0).unsqueeze(0)

        self.set_phase_change(phase, self.wvl)

    def set_zone_plate(self, radius_R, radius_C, focal_length):

        X = np.linspace(0, radius_C, self.C)
        Y = np.linspace(0, radius_R, self.R)

        xv, yv = np.meshgrid(X, Y)

        # Radius from center
        rxv = (xv - radius_C/2)
        ryv = (yv - radius_R/2)

        r = 2 * np.hypot(rxv, ryv)  # Half width as radius

        phase = radius2phase(r, focal_length, self.wvl)
        phase = torch.tensor(phase.astype(np.float32), device=self.device).unsqueeze(0).unsqueeze(0)

        self.set_phase_change(phase, self.wvl)

        # transmission = np.zeros((self.R, self.C))
        # height = np.zeros((self.R, self.C))
        # transmission[:, :] = 1
        # height_phase = (2 * np.pi) - phase
        # height[:, :] = phase2height(height_phase, self.wvl, self.material.get_RI(self.wvl))

        # height[r > radius] = 0
        # phase[r > radius] = 0

        # return height

    def resize(self, target_pitch):
        scale_factor = self.pitch / target_pitch
        super().resize(target_pitch)

        if self.mode == 'phase':
            super().resize(target_pitch)
        elif self.mode == 'height':
            self.set_height(F.interpolate(self.height, scale_factor=scale_factor, mode='bilinear', align_corners=False))
        else:
            NotImplementedError('Mode is not set.')

    def get_height(self):
        if self.mode == 'height':
            return self.height
        elif self.mode == 'phase':
            height = phase2height(self.phase_change, self.wvl, self.material.get_RI(self.wvl))
            return height
        else:
            NotImplementedError('Mode is not set.')

    def get_phase_change(self):
        if self.mode == 'height':
            self.to_phase_mode()
        return self.phase_change

    def set_height(self, height):
        if self.mode == 'height':
            self.height = height
        elif self.mode == 'phase':
            self.set_phase_change(height2phase(height, self.wvl, self.material.get_RI(self.wvl)), self.wvl)

    def set_phase_change(self, phase_change, wvl):
        if self.mode == 'height':
            self.set_height(phase2height(phase_change, wvl, self.material.get_RI(wvl)))
        if self.mode == 'phase':
            self.wvl = wvl
            self.phase_change = phase_change

    def to_phase_mode(self):
        if self.mode == 'height':
            self.phase_change = height2phase(self.height, self.wvl, self.material.get_RI(self.wvl))
            self.mode = 'phase'
            self.height = None

    def to_height_mode(self):
        if self.mode == 'phase':
            self.height = phase2height(self.phase_change, self.wvl, self.material.get_RI(self.wvl))
            self.mode = 'height'


class SLM(OpticalElement):
    def __init__(self, R, C, pitch, wvl, device, B=1):
        super().__init__(R, C, pitch, wvl, device, name="SLM", B=B)

    def set_lens(self, focal_length, shift_x=0, shift_y=0):
        '''
        focal_length: in meter
        shift_x: in meter
        shift_y: in meter
        '''

        x = np.arange(-self.C*self.pitch/2, self.C*self.pitch/2, self.pitch)
        y = np.arange(-self.R*self.pitch/2, self.R*self.pitch/2, self.pitch)
        xx,yy = np.meshgrid(x,y)

        phase = (2*np.pi / self.wvl)*((xx-shift_x)**2 + (yy-shift_y)**2) / (2*focal_length)
        phase = torch.tensor(phase.astype(np.float32), device=self.device).unsqueeze(0).unsqueeze(0)
        phase = phase % (2*np.pi)
        phase -= np.pi

        self.set_phase_change(phase, self.wvl)

    def set_amplitude_change(self, amplitude, wvl):
        self.wvl = wvl
        super().set_amplitude_change(amplitude)

    def set_phase_change(self, phase_change, wvl):
        self.wvl = wvl
        super().set_phase_change(phase_change)


class Aperture(OpticalElement):
    def __init__(self, R, C, pitch, aperture_diameter, aperture_shape, wvl, device='cpu'):

        super().__init__(R, C, pitch, wvl, device, name="aperture")

        self.aperture_diameter = aperture_diameter
        self.aperture_shape = aperture_shape
        self.amplitude_change = torch.zeros((self.R, self.C), device=device)
        if self.aperture_shape == 'square':
            self.set_square()
        elif self.aperture_shape == 'circle':
            self.set_circle()
        else:
            return NotImplementedError

    def set_square(self):
        self.aperture_shape = 'square'

        [x, y] = np.mgrid[-self.R // 2:self.R // 2, -self.C // 2:self.C // 2].astype(np.float32)
        r = self.pitch * np.asarray([abs(x), abs(y)]).max(axis=0)
        r = np.expand_dims(np.expand_dims(r, axis=0), axis=0)

        max_val = self.aperture_diameter / 2
        amp = (r <= max_val).astype(np.float32)
        amp[amp == 0] = 1e-20  # to enable stable learning
        self.amplitude_change = torch.tensor(amp, device=self.device)

    def set_circle(self, cx=0, cy=0, dia=None):
        '''
        cx, cy: relative center position of the circle with respect to the center of the grid
        aperture_diameter: circle diameter
        '''
        [x, y] = np.mgrid[-self.R // 2:self.C // 2, -self.R // 2:self.C // 2].astype(np.float32)
        r2 = (x-cx) ** 2 + (y-cy) ** 2
        r2[r2 < 0] = 1e-20
        r = self.pitch * np.sqrt(r2)
        r = np.expand_dims(np.expand_dims(r, axis=0), axis=0)
        
        if dia is not None:
            self.aperture_diameter = dia
        self.aperture_shape = 'circle'
        max_val = self.aperture_diameter / 2
        amp = (r <= max_val).astype(np.float32)
        amp[amp == 0] = 1e-20
        self.amplitude_change = torch.tensor(amp, device=self.device)

def quantize(x, levels, vmin=None, vmax=None, include_vmax=True):
    """
    include_vmax: False: quantize x with the space of 1/levels-1. 
    include_vmax: True: quantize x with the space of 1/levels 
    """

    if include_vmax is False:
        if levels == 0:
            return x

        if vmin is None:
            vmin = x.min()
        if vmax is None:
            vmax = x.max()

        #assert(vmin <= vmax)

        normalized = (x - vmin) / (vmax - vmin + 1e-16)
        if type(x) is np.ndarray:
            levelized = np.floor(normalized * levels) / (levels - 1)
        elif type(x) is torch.tensor:    
            levelized = (normalized * levels).floor() / (levels - 1)
        result = levelized * (vmax - vmin) + vmin
        result[result < vmin] = vmin
        result[result > vmax] = vmax
    
    elif include_vmax is True:
        space = (x.max()-x.min())/levels
        vmin = x.min()
        vmax = vmin + space*(levels-1)
        if type(x) is np.ndarray:
            result = (np.floor((x-vmin)/space))*space + vmin
        elif type(x) is torch.tensor:    
            result = (((x-vmin)/space).floor())*space + vmin
        result[result<vmin] = vmin
        result[result>vmax] = vmax
    
    return result