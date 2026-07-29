#!/usr/bin/env Rscript
# GEPER-authored driver script -- NOT part of the vendored upstream
# SPiP source (SPiPv2.1_main.r and everything under RefFiles/ in this
# directory are unmodified, verbatim copies of the official
# github.com/LBGC-CFB/SPiP repository; see this package's own
# __init__.py for the vendoring policy). This file exists ONLY to
# work around a real Windows-specific bug in SPiPv2.1_main.r's own
# `foreach(...) %dopar% {...}` scoring loop:
#
#   doParallel::registerDoParallel(threads) creates a PSOCK cluster on
#   Windows (fork()-based clusters, which DO inherit the whole parent
#   process's memory/globals, aren't available on Windows at all) --
#   each worker is a brand-new R process that does NOT automatically
#   see top-level script variables like `fileFormat` that
#   SPiP_functions.r's own getAnnotation()/getOutputToSPiPmodel()
#   depend on (confirmed directly: calling those functions from a
#   PSOCK worker without `fileFormat` in scope raises
#   `object 'fileFormat' not found`, silently caught by
#   getOutputToSPiPmodel's own internal tryCatch, which is why running
#   SPiPv2.1_main.r as documented produces an all-"NA"/SPiPscore=-1
#   row for every single variant on Windows -- verified against the
#   upstream repo's own testVar.vcf, not just a GEPER-constructed
#   input).
#
# Rather than hand-patching the vendored SPiPv2.1_main.r (against this
# project's own vendoring policy: re-vendor from upstream if it ever
# changes, never hand-edit), this driver reuses the exact same,
# unmodified library functions from RefFiles/SPiP_libs/SPiP_functions.r
# (readVCF, setTranscript, getVariantInfo, getAnnotation, SPiP --
# none of which use %dopar% themselves, confirmed by grepping that
# file) directly, in-process, with a plain serial loop instead of
# SPiPv2.1_main.r's own %dopar% one. For GEPER's per-variant plugin
# call pattern (exactly one variant per invocation, see
# pipeline/models/spip_plugin.py), there is no parallelism to lose --
# this only removes a multi-process cluster that would have had
# nothing to parallelize across anyway.
#
# The setup block below (library()s, source(), loading model.RData/
# RefFiles.RData/dataRefSeq<genome>.RData/transcriptome_<genome>.RData/
# VPP+VPN tables, and the SPiCE/MES/ESR constant derivations) is
# copied from SPiPv2.1_main.r's own setup section (same MIT-licensed
# project, same author's own code) purely because SPiP_functions.r's
# functions require those exact globals to already be populated in
# the calling environment -- nothing about the scoring logic itself
# is changed, re-derived, or reimplemented here.

suppressPackageStartupMessages({
    library(parallel)
    library(foreach)
    library(doParallel)
    library(randomForest)
})

options(scipen = 50, stringsAsFactors = FALSE)

argsFull <- commandArgs(trailingOnly = TRUE)
argMap <- list()
i <- 1
while (i <= length(argsFull)) {
    argMap[[argsFull[i]]] <- argsFull[i + 1]
    i <- i + 2
}

inputFile <- normalizePath(argMap[["--input"]])
outputFile <- argMap[["--output"]]
genome <- argMap[["--GenomeAssenbly"]]
runtimeDir <- normalizePath(argMap[["--runtimeDir"]])

fileFormat <- "vcf"
printProcess <- FALSE
threads <- 1
maxLines <- 1000
pathToGene <- NULL
pathToTranscript <- NULL

inputref <- paste0(runtimeDir, "/RefFiles")
path2scripts <- paste0(inputref, "/SPiP_libs/")
source(paste0(path2scripts, "SPiP_functions.r"))

path2headers <- paste0(inputref, "/headers/")
headerHelp <- readLines(paste0(path2headers, "headerHelp.txt"))

cat("Loading transcriptome...\n")
load(paste0(inputref, "/transcriptome_", genome, ".RData"))
cat("Loading model...\n")
load(paste0(inputref, "/model.RData"))
VPPtable <- read.table(paste0(inputref, "/VPP_table.txt"), sep = "\t", dec = ",", header = TRUE)
VPNtable <- read.table(paste0(inputref, "/VPN_table.txt"), sep = "\t", dec = ",", header = TRUE)
load(paste0(inputref, "/dataRefSeq", genome, ".RData"))
load(paste0(inputref, "/RefFiles.RData"))

# -- constants block, copied verbatim from SPiPv2.1_main.r --
mint_GT <- sum(as.numeric(as.vector(sub("Min.   :", "", summary(ref_score_GT)[1, ]))))
maxt_GT <- sum(as.numeric(as.vector(sub("Max.   :", "", summary(ref_score_GT)[6, ]))))
mint_GC <- sum(as.numeric(as.vector(sub("Min.   :", "", summary(ref_score_GC)[1, ]))))
maxt_GC <- sum(as.numeric(as.vector(sub("Max.   :", "", summary(ref_score_GC)[6, ]))))
mint1 <- as.numeric(as.vector(sub("Min.   :", "", summary(ref_score_AG)[1, 1:10])))
maxt1 <- as.numeric(as.vector(sub("Max.   :", "", summary(ref_score_AG)[6, 1:10])))
maxt1 <- maxt1[order(maxt1, decreasing = TRUE)]
maxt1 <- sum(maxt1[1:8])
mint1 <- mint1[order(mint1, decreasing = FALSE)]
mint1 <- sum(mint1[1:8])
mint2 <- sum(as.numeric(as.vector(sub("Min.   :", "", summary(ref_score_AG)[1, 12:15]))))
maxt2 <- sum(as.numeric(as.vector(sub("Max.   :", "", summary(ref_score_AG)[6, 12:15]))))
i_score <- NULL
i_score1 <- NULL
i_score2 <- NULL
dataESR$hexamer <- as.character(dataESR$hexamer)
ESRmotif <- dataESR$hexamer[dataESR$Assignment != "N"]
LEIsc_valuesWA <- dataESR$LEIsc_valuesWA
LEIsc_valuesHA <- dataESR$LEIsc_valuesHA
LEIsc_valuesHM <- dataESR$LEIsc_valuesHM
LEIsc_valuesWD <- dataESR$LEIsc_valuesWD
LEIsc_valuesHD <- dataESR$LEIsc_valuesHD
ESRlistScore <- dataESR$ESEseq_or_ESSseqscore
names(LEIsc_valuesWA) <- dataESR$hexamer
names(LEIsc_valuesHA) <- dataESR$hexamer
names(LEIsc_valuesHM) <- dataESR$hexamer
names(LEIsc_valuesWD) <- dataESR$hexamer
names(LEIsc_valuesHD) <- dataESR$hexamer
names(ESRlistScore) <- dataESR$hexamer
indAcc <- c(1:11, 17:62, 68:95)
indDon <- c(1:28, 34:74, 80:95)
me2x5 <- ME2x5$V1
names(me2x5) <- as.character(ME2x5$V1.1)
inverseDic <- data.frame(V1 = c("T", "G", "C", "A", "N"), row.names = c("A", "C", "G", "T", "N"))
inverseDic$V1 <- as.character(inverseDic$V1)
RegTypeToNumber <- data.frame(
    V1 = c(1:11),
    row.names = c(
        "DeepIntron", "Exon", "ExonESR", "ExonESRCons", "Intron",
        "IntronBP", "IntronCons", "IntronConsPolyTC", "IntronConsPolyTCBP",
        "IntronPolyTC", "IntronPolyTCBP"
    )
)
thToSPiPexon <- 0.18
thToSPiPintron <- 0.035
thToComplexEvent <- 0.2

# -- read the (small, single-block) input VCF --
input <- file(inputFile, "r")
rawInput <- readLines(input, n = 1)
while (as.numeric(regexpr("^#CHROM", rawInput)) != 1) {
    rawInput <- readLines(input, n = 1)
}
columnsNames <- unlist(strsplit(rawInput, split = "\t"))
columnsNames[1] <- substr(columnsNames[1], 2, nchar(columnsNames[1]))
lengthDataLine <- length(columnsNames)
rawInput <- readLines(input)
close(input)

cat("Extracting sequences and variant informations...\n")
tmpVCF <- lapply(seq_along(rawInput), function(i) readVCF(rawInput[i], i))
unlisted <- as.data.frame(matrix(unlist(tmpVCF), ncol = 5 + lengthDataLine, byrow = TRUE))
data <- unlisted[, c(1, 2, 3, 5)]
names(data) <- c("varID", "seqPhysio", "seqMutated", "altUsed")
transcriptsDF <- data.frame(trID = unlisted[, c(4)])
VCFinfo_DF <- unlisted[, c(6:(5 + lengthDataLine))]
colnames(VCFinfo_DF) <- columnsNames
VCFinfo_text <- apply(VCFinfo_DF, 1, paste, collapse = "\t")

setTranscript(transcriptsDF)

cat("Score calculation...\n")
rawResult <- lapply(seq_len(nrow(data)), function(i) {
    getOutputToSPiPmodel(data[i, "varID"], i, data[i, "seqPhysio"], data[i, "seqMutated"])
})
rawAnnotation <- as.data.frame(matrix(unlist(rawResult), ncol = 35, byrow = TRUE))
names(rawAnnotation) <- c(
    "chr", "strand", "gNomen", "varType", "ntChange", "ExonInfo", "exonSize", "transcript",
    "gene", "NearestSS", "DistSS", "RegType", "seqPhysio", "seqMutated", "SPiCEproba", "SPiCEinter_2thr",
    "deltaMES", "BP", "mutInPBarea", "deltaESRscore", "posCryptMut", "sstypeCryptMut", "probaCryptMut",
    "classProbaCryptMut", "nearestSStoCrypt", "nearestPosSStoCrypt", "nearestDistSStoCrypt", "posCryptWT",
    "probaCryptWT", "classProbaCryptWT", "posSSPhysio", "probaSSPhysio", "classProbaSSPhysio",
    "probaSSPhysioMut", "classProbaSSPhysioMut"
)
data <- cbind(data, rawAnnotation)
data <- SPiP(data)

cat("Writing results...\n")
output <- file(outputFile, "w")
writeLines(
    paste(paste(columnsNames, collapse = "\t"), "varID", "Interpretation", "InterConfident", "SPiPscore", "strand",
        "gNomen", "varType", "ntChange", "ExonInfo", "exonSize", "transcript", "gene", "NearestSS", "DistSS", "RegType",
        "SPiCEproba", "SPiCEinter_2thr", "deltaMES", "BP", "mutInPBarea", "deltaESRscore", "posCryptMut",
        "sstypeCryptMut", "probaCryptMut", "classProbaCryptMut", "nearestSStoCrypt", "nearestPosSStoCrypt",
        "nearestDistSStoCrypt", "posCryptWT", "probaCryptWT", "classProbaCryptWT", "posSSPhysio", "probaSSPhysio",
        "classProbaSSPhysio", "probaSSPhysioMut", "classProbaSSPhysioMut",
        sep = "\t"
    ),
    con = output, sep = "\n"
)
rawResultText <- apply(data[, c(
    "varID", "Interpretation", "InterConfident", "SPiPscore", "strand", "gNomen", "varType", "ntChange", "ExonInfo",
    "exonSize", "transcript", "gene", "NearestSS", "DistSS", "RegType", "SPiCEproba", "SPiCEinter_2thr", "deltaMES",
    "BP", "mutInPBarea", "deltaESRscore", "posCryptMut", "sstypeCryptMut", "probaCryptMut", "classProbaCryptMut",
    "nearestSStoCrypt", "nearestPosSStoCrypt", "nearestDistSStoCrypt", "posCryptWT", "probaCryptWT", "classProbaCryptWT",
    "posSSPhysio", "probaSSPhysio", "classProbaSSPhysio", "probaSSPhysioMut", "classProbaSSPhysioMut"
)], 1, paste, collapse = "\t")
writeLines(paste(VCFinfo_text, rawResultText, sep = "\t"), con = output, sep = "\n")
close(output)
cat("Done.\n")
