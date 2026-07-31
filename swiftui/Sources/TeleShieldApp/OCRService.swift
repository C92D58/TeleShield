import Foundation

#if canImport(Vision)
import CoreGraphics
import ImageIO
import Vision
#endif

enum OCRService {
    static var status: OCRStatus {
        #if canImport(Vision)
        return OCRStatus(available: true, bundled: false, languages: ["eng", "chi_sim", "chi_tra"])
        #else
        return OCRStatus(available: false, bundled: false, languages: [])
        #endif
    }

    static func recognizeText(in imageData: Data) async throws -> String {
        #if canImport(Vision)
        guard let source = CGImageSourceCreateWithData(imageData as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw CoreClientError(message: "OCR 無法讀取圖片")
        }
        return try await Task.detached(priority: .utility) {
            try recognize(image: image)
        }.value
        #else
        throw CoreClientError(message: "此平台沒有 Vision OCR")
        #endif
    }

    #if canImport(Vision)
    private static func recognize(image: CGImage) throws -> String {
        var result: Result<String, Error>?
        let request = VNRecognizeTextRequest { request, error in
            if let error {
                result = .failure(error)
                return
            }
            let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
            result = .success(observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n"))
        }
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hant", "zh-Hans", "en-US"]
        request.usesLanguageCorrection = true
        try VNImageRequestHandler(cgImage: image).perform([request])
        return try result?.get() ?? ""
    }
    #endif
}
